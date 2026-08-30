"""EasyDict-compatible SQLite builders and read-back verification."""

from __future__ import annotations

import json
import random
import sqlite3
import unicodedata
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import zstandard as zstd

ZSTD_SAMPLE_SIZE = 10_000
ZSTD_DICTIONARY_SIZE = 112 * 1024
ZSTD_COMPRESSION_LEVEL = 7
ZSTD_RANDOM_SEED = 0x0A1D10
MIN_TRAINING_SAMPLES = 7
DATABASE_BATCH_SIZE = 1000
MP3_MIN_HEADER_SIZE = 2
MP3_SYNC_BYTE = 0xFF
MP3_SYNC_MASK = 0xE0


def normalize_headword(value: str) -> str:
    """Normalize an exact-query headword consistently at build and runtime."""

    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(normalized.split())


def file_crc32(path: Path) -> str:
    """Return an eight-character lowercase CRC32 for a file."""

    checksum = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum = zlib.crc32(chunk, checksum)
    return f"{checksum & 0xFFFFFFFF:08x}"


def _reservoir_samples(path: Path) -> list[bytes]:
    rng = random.Random(ZSTD_RANDOM_SEED)  # noqa: S311 - deterministic build sampling
    samples: list[bytes] = []
    seen = 0
    with path.open("rb") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue
            if len(samples) < ZSTD_SAMPLE_SIZE:
                samples.append(line)
            else:
                replacement = rng.randint(0, seen)
                if replacement < ZSTD_SAMPLE_SIZE:
                    samples[replacement] = line
            seen += 1
    if len(samples) < MIN_TRAINING_SAMPLES:
        raise RuntimeError("Zstd dictionary training needs at least seven entries")
    return samples


def _index_rows(
    entry: dict[str, Any],
    aliases: Iterable[str],
) -> list[tuple[str, str, str, str, int, str]]:
    entry_id = int(entry["entry_id"])
    entry_type = str(entry["entry_type"])
    phonetic = " ".join(
        str(item.get("notation", "")) for item in entry.get("pronunciation", [])
    ).strip()
    candidates: list[tuple[str, str]] = [(str(entry["headword"]), "")]
    candidates.extend((alias, "") for alias in aliases)
    for field in ("child_idioms", "child_phrasal_verbs", "child_derivatives"):
        for index, child in enumerate(entry.get(field, [])):
            child_headword = child.get("headword")
            if child_headword:
                candidates.append((str(child_headword), f"{field}.{index}"))

    result: list[tuple[str, str, str, str, int, str]] = []
    seen: set[tuple[str, str]] = set()
    for headword, anchor in candidates:
        normalized = normalize_headword(headword)
        key = (normalized, anchor)
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append((headword, normalized, phonetic, entry_type, entry_id, anchor))
    return result


def build_dictionary_db(
    jsonl_path: Path,
    db_path: Path,
    aliases_by_entry: dict[int, set[str]],
    *,
    dictionary_size: int = ZSTD_DICTIONARY_SIZE,
) -> dict[str, int]:
    """Build the EasyDict config/entries/indices/groups schema from JSONL."""

    samples = _reservoir_samples(jsonl_path)
    training_samples: list[bytes | bytearray | memoryview[int]] = list(samples)
    trained = zstd.train_dictionary(dictionary_size, training_samples)
    compressor = zstd.ZstdCompressor(dict_data=trained, level=ZSTD_COMPRESSION_LEVEL)
    del samples

    connection = sqlite3.connect(db_path)
    entry_count = 0
    index_count = 0
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value BLOB)")
        connection.execute(
            "CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, json_data BLOB NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE indices (
                id INTEGER PRIMARY KEY,
                headword TEXT NOT NULL,
                headword_normalized TEXT NOT NULL,
                phonetic TEXT,
                entry_type TEXT,
                entry_id INTEGER NOT NULL,
                anchor TEXT,
                FOREIGN KEY (entry_id) REFERENCES entries(entry_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE groups (
                group_id TEXT PRIMARY KEY,
                parent_id TEXT,
                name TEXT NOT NULL,
                description TEXT,
                item_list TEXT DEFAULT '[]',
                sub_group_count INTEGER DEFAULT 0,
                item_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_id) REFERENCES groups(group_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("INSERT INTO config VALUES ('zstd_dict', ?)", (trained.as_bytes(),))

        entries_batch: list[tuple[int, bytes]] = []
        indices_batch: list[tuple[str, str, str, str, int, str]] = []
        with jsonl_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                entry_id = int(entry["entry_id"])
                canonical = json.dumps(
                    entry,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                entries_batch.append((entry_id, compressor.compress(canonical)))
                indices_batch.extend(_index_rows(entry, aliases_by_entry.get(entry_id, set())))
                if len(entries_batch) >= DATABASE_BATCH_SIZE:
                    connection.executemany("INSERT INTO entries VALUES (?, ?)", entries_batch)
                    connection.executemany(
                        """
                        INSERT INTO indices
                            (headword, headword_normalized, phonetic, entry_type, entry_id, anchor)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        indices_batch,
                    )
                    entry_count += len(entries_batch)
                    index_count += len(indices_batch)
                    entries_batch.clear()
                    indices_batch.clear()
        if entries_batch:
            connection.executemany("INSERT INTO entries VALUES (?, ?)", entries_batch)
            connection.executemany(
                """
                INSERT INTO indices
                    (headword, headword_normalized, phonetic, entry_type, entry_id, anchor)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                indices_batch,
            )
            entry_count += len(entries_batch)
            index_count += len(indices_batch)

        connection.execute("CREATE INDEX idx_headword_norm ON indices(headword_normalized)")
        connection.execute("CREATE INDEX idx_phonetic ON indices(phonetic)")
        connection.execute("CREATE INDEX idx_indices_entry_id ON indices(entry_id)")
        connection.execute("CREATE INDEX idx_groups_parent ON groups(parent_id)")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return {"entries": entry_count, "indices": index_count}


def verify_dictionary_db(path: Path) -> dict[str, int]:
    """Validate schema, foreign keys, and every Zstd-compressed JSON entry."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        expected = {"config", "entries", "indices", "groups"}
        if tables != expected:
            raise RuntimeError(f"dictionary schema mismatch: {sorted(tables)}")
        row = connection.execute("SELECT value FROM config WHERE key = 'zstd_dict'").fetchone()
        if row is None or not row[0]:
            raise RuntimeError("dictionary has no Zstd dictionary")
        decompressor = zstd.ZstdDecompressor(dict_data=zstd.ZstdCompressionDict(bytes(row[0])))
        entry_count = 0
        for entry_id, compressed in connection.execute(
            "SELECT entry_id, json_data FROM entries ORDER BY entry_id"
        ):
            entry = json.loads(decompressor.decompress(compressed))
            if int(entry["entry_id"]) != entry_id:
                raise RuntimeError(f"entry ID mismatch for {entry_id}")
            entry_count += 1
        dangling = connection.execute(
            """
            SELECT COUNT(*) FROM indices i
            LEFT JOIN entries e ON e.entry_id = i.entry_id
            WHERE e.entry_id IS NULL
            """
        ).fetchone()[0]
        if dangling:
            raise RuntimeError(f"dictionary has {dangling} dangling indices")
        index_count = connection.execute("SELECT COUNT(*) FROM indices").fetchone()[0]
    finally:
        connection.close()
    return {"entries": int(entry_count), "indices": int(index_count)}


def verify_media_db(path: Path) -> dict[str, int]:
    """Validate media schema, unique names, MP3 headers, and BLOB CRC reads."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    checksum = 0
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if tables != {"audios", "images"}:
            raise RuntimeError(f"media schema mismatch: {sorted(tables)}")
        audio_count = 0
        for name, blob in connection.execute("SELECT name, blob FROM audios ORDER BY name"):
            if not name.casefold().endswith(".mp3"):
                raise RuntimeError(f"non-MP3 media row: {name!r}")
            data = bytes(blob)
            if not (
                data.startswith(b"ID3")
                or (
                    len(data) >= MP3_MIN_HEADER_SIZE
                    and data[0] == MP3_SYNC_BYTE
                    and data[1] & MP3_SYNC_MASK == MP3_SYNC_MASK
                )
            ):
                raise RuntimeError(f"invalid MP3 media row: {name!r}")
            checksum = zlib.crc32(data, checksum)
            audio_count += 1
        image_count = connection.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    finally:
        connection.close()
    return {
        "audios": int(audio_count),
        "images": int(image_count),
        "combined_crc32": checksum & 0xFFFFFFFF,
    }
