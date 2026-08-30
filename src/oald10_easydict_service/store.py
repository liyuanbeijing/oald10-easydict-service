"""Read-only access to generated EasyDict SQLite databases."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import zstandard as zstd

from oald10_easydict_service.database import normalize_headword


class StoreUnavailableError(RuntimeError):
    """Raised when generated dictionary data cannot be opened or queried."""


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


class DictionaryStore:
    """Two immutable SQLite connections and a shared Zstd decompressor."""

    def __init__(self, data_path: Path) -> None:
        """Configure paths below an EasyDict data root."""
        self.dictionary_dir = data_path / "dictionaries" / "oald10"
        self.dictionary_path = self.dictionary_dir / "dictionary.db"
        self.media_path = self.dictionary_dir / "media.db"
        self.metadata_path = self.dictionary_dir / "metadata.json"
        self._lock = threading.RLock()
        self._dictionary: sqlite3.Connection | None = None
        self._media: sqlite3.Connection | None = None
        self._decompressor: zstd.ZstdDecompressor | None = None
        self._metadata: dict[str, Any] = {}
        self._entry_count = 0
        self._audio_count = 0

    def open(self) -> None:
        """Open and validate both databases without ever enabling writes."""

        required = (self.dictionary_path, self.media_path, self.metadata_path)
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise StoreUnavailableError(f"missing generated data: {', '.join(missing)}")
        try:
            dictionary = _open_read_only(self.dictionary_path)
            media = _open_read_only(self.media_path)
            dictionary_tables = {
                row[0]
                for row in dictionary.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if dictionary_tables != {"config", "entries", "indices", "groups"}:
                raise StoreUnavailableError("dictionary.db has an incompatible schema")
            media_tables = {
                row[0]
                for row in media.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if media_tables != {"audios", "images"}:
                raise StoreUnavailableError("media.db has an incompatible schema")
            zstd_row = dictionary.execute(
                "SELECT value FROM config WHERE key = 'zstd_dict'"
            ).fetchone()
            if zstd_row is None or not zstd_row[0]:
                raise StoreUnavailableError("dictionary.db has no Zstd dictionary")
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            self._entry_count = int(
                dictionary.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            )
            self._audio_count = int(media.execute("SELECT COUNT(*) FROM audios").fetchone()[0])
            self._decompressor = zstd.ZstdDecompressor(
                dict_data=zstd.ZstdCompressionDict(bytes(zstd_row[0]))
            )
            self._dictionary = dictionary
            self._media = media
            self._metadata = metadata
            self.ping()
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
            self.close()
            raise StoreUnavailableError(str(error)) from error

    def close(self) -> None:
        """Close any database handles opened during application lifespan."""

        with self._lock:
            if self._dictionary is not None:
                self._dictionary.close()
            if self._media is not None:
                self._media.close()
            self._dictionary = None
            self._media = None
            self._decompressor = None

    def _require_open(
        self,
    ) -> tuple[sqlite3.Connection, sqlite3.Connection, zstd.ZstdDecompressor]:
        if self._dictionary is None or self._media is None or self._decompressor is None:
            raise StoreUnavailableError("dictionary store is not open")
        return self._dictionary, self._media, self._decompressor

    def ping(self) -> None:
        """Raise when either immutable database is no longer readable."""

        with self._lock:
            dictionary, media, _ = self._require_open()
            dictionary.execute("SELECT 1 FROM entries LIMIT 1").fetchone()
            media.execute("SELECT 1 FROM audios LIMIT 1").fetchone()

    def info(self) -> dict[str, Any]:
        """Return dictionary metadata with authoritative live counts and sizes."""

        return {
            "id": "oald10",
            "name": self._metadata.get(
                "name", "Oxford Advanced Learner's Dictionary 10th Bilingual"
            ),
            "version": int(self._metadata.get("version", 1)),
            "entry_count": self._entry_count,
            "audio_count": self._audio_count,
            "image_count": 0,
            "dict_size": self.dictionary_path.stat().st_size,
            "media_size": self.media_path.stat().st_size,
        }

    @staticmethod
    def _decode(blob: bytes, decompressor: zstd.ZstdDecompressor) -> dict[str, Any]:
        return json.loads(decompressor.decompress(bytes(blob)))

    def query_word(self, word: str) -> list[dict[str, Any]]:
        """Return every exact Unicode-normalized, case-insensitive headword hit."""

        normalized = normalize_headword(word)
        with self._lock:
            dictionary, _, decompressor = self._require_open()
            rows = dictionary.execute(
                """
                SELECT DISTINCT e.entry_id, e.json_data
                FROM indices AS i
                JOIN entries AS e ON e.entry_id = i.entry_id
                WHERE i.headword_normalized = ?
                ORDER BY e.entry_id
                LIMIT 100
                """,
                (normalized,),
            ).fetchall()
            return [self._decode(row["json_data"], decompressor) for row in rows]

    def query_entry(self, entry_id: int) -> dict[str, Any] | None:
        """Return one entry by stable integer ID."""

        with self._lock:
            dictionary, _, decompressor = self._require_open()
            row = dictionary.execute(
                "SELECT json_data FROM entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            return self._decode(row["json_data"], decompressor) if row else None

    def query_audio(self, filename: str) -> bytes | None:
        """Return one word-audio BLOB by safe basename."""

        with self._lock:
            _, media, _ = self._require_open()
            row = media.execute(
                "SELECT blob FROM audios WHERE name = ?",
                (filename,),
            ).fetchone()
            return bytes(row["blob"]) if row else None
