"""Streaming MDX access and selective MDD record-block extraction."""

from __future__ import annotations

import bisect
import sqlite3
import zlib
from collections import defaultdict
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mdict_utils.base.readmdict import MDD, MDX

REDIRECT_PREFIX = "@@@LINK="
MP3_MIN_HEADER_SIZE = 2
MP3_SYNC_BYTE = 0xFF
MP3_SYNC_MASK = 0xE0
MDICT_VERSION_3 = 3


@dataclass(frozen=True, slots=True)
class MDXRecord:
    """A one-based MDX record with its decoded key and content."""

    number: int
    key: str
    content: str

    @property
    def redirect_target(self) -> str | None:
        """Return the target key when this is an MDict redirect record."""

        stripped = self.content.strip("\x00\r\n ")
        if stripped.startswith(REDIRECT_PREFIX):
            return stripped.removeprefix(REDIRECT_PREFIX).strip()
        return None


def iter_mdx_records(path: Path) -> Generator[MDXRecord, None, None]:
    """Yield all records through ``MDX.items()`` without materializing HTML."""

    mdx = MDX(str(path))
    for number, (key, value) in enumerate(mdx.items(), start=1):
        yield MDXRecord(
            number=number,
            key=key.decode("utf-8"),
            content=value.decode("utf-8", errors="strict"),
        )


def mdx_record_count(path: Path) -> int:
    """Read the MDX key index and return its record count."""

    return len(MDX(str(path)))


def _mdd_key(filename: str) -> str:
    return f"\\audio\\word\\{filename}".casefold()


def _valid_mp3(data: bytes) -> bool:
    return data.startswith(b"ID3") or (
        len(data) >= MP3_MIN_HEADER_SIZE
        and data[0] == MP3_SYNC_BYTE
        and data[1] & MP3_SYNC_MASK == MP3_SYNC_MASK
    )


def _record_block_layout(mdd: Any) -> tuple[list[tuple[int, int, int, int]], int]:
    """Return (decompressed start, size, compressed start, size) per MDD block."""

    if mdd._version >= MDICT_VERSION_3:  # noqa: SLF001 - no public block index API
        raise RuntimeError("OALD10 media requires the verified MDict 2.x layout")
    layout: list[tuple[int, int, int, int]] = []
    with Path(mdd._fname).open("rb") as stream:  # noqa: SLF001
        stream.seek(mdd._record_block_offset)  # noqa: SLF001
        block_count = mdd._read_number(stream)  # noqa: SLF001
        entry_count = mdd._read_number(stream)  # noqa: SLF001
        if entry_count != len(mdd):
            raise RuntimeError("MDD record count does not match its record-block header")
        info_size = mdd._read_number(stream)  # noqa: SLF001
        total_compressed_size = mdd._read_number(stream)  # noqa: SLF001
        block_info = [
            (
                (mdd._read_number(stream), mdd._read_number(stream))  # noqa: SLF001
            )
            for _ in range(block_count)
        ]
        if (
            stream.tell() - mdd._record_block_offset - (mdd._number_width * 4)  # noqa: SLF001
            != info_size
        ):
            raise RuntimeError("invalid MDD record-block info size")
        compressed_start = stream.tell()
        decompressed_start = 0
        for compressed_size, decompressed_size in block_info:
            layout.append(
                (
                    decompressed_start,
                    decompressed_size,
                    compressed_start,
                    compressed_size,
                )
            )
            decompressed_start += decompressed_size
            compressed_start += compressed_size
        if sum(item[3] for item in layout) != total_compressed_size:
            raise RuntimeError("invalid MDD total compressed record size")
    return layout, decompressed_start


def import_selected_audio(  # noqa: C901 - block grouping keeps memory and I/O bounded
    mdd_path: Path,
    filenames: set[str],
    media_db_path: Path,
    *,
    batch_size: int = 512,
) -> dict[str, int | list[str]]:
    """Decode only MDD blocks containing referenced word audio and write SQLite BLOBs."""

    mdd = MDD(str(mdd_path))
    layout, total_decompressed_size = _record_block_layout(mdd)
    block_starts = [item[0] for item in layout]
    wanted = {_mdd_key(filename): filename for filename in filenames}
    records_by_block: dict[int, list[tuple[str, int, int]]] = defaultdict(list)
    duplicate_keys: list[str] = []
    located: set[str] = set()

    key_list = mdd._key_list  # noqa: SLF001 - needed to avoid unpacking unrelated MDD blocks
    for index, (offset, raw_key) in enumerate(key_list):
        decoded_key = raw_key.decode("utf-8").casefold()
        filename = wanted.get(decoded_key)
        if filename is None:
            continue
        end = key_list[index + 1][0] if index + 1 < len(key_list) else total_decompressed_size
        block_index = bisect.bisect_right(block_starts, offset) - 1
        if block_index < 0:
            raise RuntimeError(f"MDD record {decoded_key!r} precedes the first record block")
        if filename in located:
            duplicate_keys.append(filename)
        located.add(filename)
        records_by_block[block_index].append((filename, offset, end))

    missing = sorted(filenames - located)
    media_db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(media_db_path)
    checksums: dict[str, int] = {}
    try:
        connection.execute("PRAGMA page_size = 65536")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("CREATE TABLE audios (name TEXT PRIMARY KEY, blob BLOB NOT NULL)")
        connection.execute("CREATE TABLE images (name TEXT PRIMARY KEY, blob BLOB NOT NULL)")
        batch: list[tuple[str, bytes]] = []
        with mdd_path.open("rb") as stream:
            for block_index in sorted(records_by_block):
                decompressed_start, decompressed_size, compressed_start, compressed_size = layout[
                    block_index
                ]
                stream.seek(compressed_start)
                block = mdd._decode_block(stream.read(compressed_size), decompressed_size)  # noqa: SLF001
                for filename, start, end in records_by_block[block_index]:
                    data = block[start - decompressed_start : end - decompressed_start]
                    if not _valid_mp3(data):
                        raise RuntimeError(f"invalid MP3 header for {filename!r}")
                    checksum = zlib.crc32(data) & 0xFFFFFFFF
                    previous_checksum = checksums.get(filename)
                    if previous_checksum is not None:
                        if previous_checksum != checksum:
                            raise RuntimeError(f"conflicting duplicate MDD audio: {filename!r}")
                        continue
                    checksums[filename] = checksum
                    batch.append((filename, data))
                    if len(batch) >= batch_size:
                        connection.executemany("INSERT INTO audios VALUES (?, ?)", batch)
                        batch.clear()
                if batch:
                    connection.executemany("INSERT INTO audios VALUES (?, ?)", batch)
                    batch.clear()
        connection.execute("CREATE INDEX idx_audios_name ON audios(name)")
        connection.execute("CREATE INDEX idx_images_name ON images(name)")
        connection.commit()
        stored_count = connection.execute("SELECT COUNT(*) FROM audios").fetchone()[0]
    finally:
        connection.close()

    return {
        "requested": len(filenames),
        "stored": int(stored_count),
        "decoded_blocks": len(records_by_block),
        "missing": missing,
        "duplicate_keys": sorted(set(duplicate_keys)),
    }
