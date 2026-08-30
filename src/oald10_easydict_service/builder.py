"""Atomic end-to-end OALD10 conversion and EasyDict database build."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oald10_easydict_service.database import (
    build_dictionary_db,
    file_crc32,
    normalize_headword,
    verify_dictionary_db,
    verify_media_db,
)
from oald10_easydict_service.mdict_source import import_selected_audio, iter_mdx_records
from oald10_easydict_service.parser import ParseError, parse_record

EXPECTED_MDX_RECORDS = 109_014
EXPECTED_REDIRECTS = 57_704
EXPECTED_UNIQUE_AUDIO = 96_395
EXPECTED_RAW_EXAMPLES = 110_243
EXPECTED_RETAINED_EXAMPLES = 101_044
EXPECTED_RETAINED_EXAMPLE_LISTS = 58_898
EXPECTED_EXCLUDED_EXAMPLES = 9_199
LOGGER = logging.getLogger(__name__)


class BuildError(RuntimeError):
    """Raised when a full build violates an audited invariant."""


@dataclass(slots=True)
class ConversionState:
    """Bounded metadata retained while entries stream to JSONL."""

    direct_entries: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    redirects: list[tuple[int, str, str]] = field(default_factory=list)
    aliases_by_entry: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    audio_references: set[str] = field(default_factory=set)
    entry_ids: set[int] = field(default_factory=set)
    audit: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "building",
            "mdx_records": 0,
            "redirects": 0,
            "entries": 0,
            "definition_pairs": 0,
            "english_only_definitions": 0,
            "chinese_only_definitions": 0,
            "cross_references": 0,
            "raw_examples": 0,
            "retained_examples": 0,
            "retained_example_lists": 0,
            "excluded_examples": 0,
            "missing_english_examples": 0,
            "missing_chinese_examples": 0,
            "orphan_examples": 0,
            "duplicate_example_imports": 0,
            "missing_uk_pronunciation": 0,
            "missing_us_pronunciation": 0,
            "id_collisions": [],
            "parse_failures": [],
            "redirect_cycles": [],
            "unresolved_redirects": [],
        }
    )


def _normalized_key(value: str) -> str:
    return normalize_headword(value)


def convert_mdx(mdx_path: Path, jsonl_path: Path) -> ConversionState:
    """Stream the entire MDX into bilingual entry JSONL plus bounded indexes."""

    state = ConversionState()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as output:
        for record in iter_mdx_records(mdx_path):
            state.audit["mdx_records"] = record.number
            if record.number % 10_000 == 0:
                LOGGER.info(
                    "converted %s MDX records into %s entries",
                    f"{record.number:,}",
                    f"{state.audit['entries']:,}",
                )
            target = record.redirect_target
            if target is not None:
                state.redirects.append((record.number, record.key, target))
                state.audit["redirects"] += 1
                continue
            try:
                parsed = parse_record(record.content, record.number, record.key)
            except (ParseError, UnicodeError, ValueError) as error:
                state.audit["parse_failures"].append(
                    {"record": record.number, "key": record.key, "reason": str(error)}
                )
                continue

            normalized_key = _normalized_key(record.key)
            metrics = parsed.metrics
            state.audit["definition_pairs"] += metrics.definition_pairs
            state.audit["english_only_definitions"] += metrics.english_only_definitions
            state.audit["chinese_only_definitions"] += metrics.chinese_only_definitions
            state.audit["cross_references"] += metrics.cross_references
            state.audit["raw_examples"] += metrics.raw_examples
            state.audit["retained_examples"] += metrics.retained_examples
            state.audit["retained_example_lists"] += metrics.retained_example_lists
            state.audit["excluded_examples"] += metrics.excluded_examples
            state.audit["missing_english_examples"] += metrics.missing_english_examples
            state.audit["missing_chinese_examples"] += metrics.missing_chinese_examples
            state.audit["orphan_examples"] += metrics.orphan_examples
            state.audit["duplicate_example_imports"] += metrics.duplicate_example_imports
            state.audio_references.update(metrics.audio_references)
            for entry in parsed.entries:
                if entry.entry_id in state.entry_ids:
                    state.audit["id_collisions"].append(entry.entry_id)
                    continue
                state.entry_ids.add(entry.entry_id)
                state.direct_entries[normalized_key].append(entry.entry_id)
                state.aliases_by_entry[entry.entry_id].add(record.key)
                regions = {item.region for item in entry.pronunciation}
                if "UK" not in regions:
                    state.audit["missing_uk_pronunciation"] += 1
                if "US" not in regions:
                    state.audit["missing_us_pronunciation"] += 1
                output.write(
                    entry.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                )
                output.write("\n")
                state.audit["entries"] += 1
    state.audit["unique_audio_references"] = len(state.audio_references)
    return state


def resolve_redirects(state: ConversionState) -> None:
    """Resolve redirect chains and attach every source key as an entry alias."""

    redirects_by_source: dict[str, list[str]] = defaultdict(list)
    for _, source, target in state.redirects:
        redirects_by_source[_normalized_key(source)].append(target)

    cache: dict[str, tuple[int, ...]] = {}
    cycles: set[tuple[str, ...]] = set()

    def resolve(key: str, stack: tuple[str, ...] = ()) -> tuple[int, ...]:
        normalized = _normalized_key(key)
        if normalized in cache:
            return cache[normalized]
        if normalized in stack:
            direct = tuple(state.direct_entries.get(normalized, []))
            if direct:
                return direct
            cycle = (*stack[stack.index(normalized) :], normalized)
            cycles.add(cycle)
            return ()
        result = list(state.direct_entries.get(normalized, []))
        next_stack = (*stack, normalized)
        for target in redirects_by_source.get(normalized, []):
            result.extend(resolve(target, next_stack))
        resolved = tuple(dict.fromkeys(result))
        cache[normalized] = resolved
        return resolved

    unresolved: list[dict[str, Any]] = []
    for record_number, source, target in state.redirects:
        entry_ids = resolve(target)
        if not entry_ids:
            unresolved.append({"record": record_number, "source": source, "target": target})
            continue
        for entry_id in entry_ids:
            state.aliases_by_entry[entry_id].add(source)
    state.audit["redirect_cycles"] = [list(cycle) for cycle in sorted(cycles)]
    state.audit["unresolved_redirects"] = unresolved


def _audit_errors(audit: dict[str, Any], *, include_media: bool) -> list[str]:
    errors: list[str] = []
    expected_counts = {
        "mdx_records": EXPECTED_MDX_RECORDS,
        "redirects": EXPECTED_REDIRECTS,
        "unique_audio_references": EXPECTED_UNIQUE_AUDIO,
        "raw_examples": EXPECTED_RAW_EXAMPLES,
        "retained_examples": EXPECTED_RETAINED_EXAMPLES,
        "retained_example_lists": EXPECTED_RETAINED_EXAMPLE_LISTS,
        "excluded_examples": EXPECTED_EXCLUDED_EXAMPLES,
    }
    for name, expected in expected_counts.items():
        if audit.get(name) != expected:
            errors.append(f"{name}: expected {expected}, got {audit.get(name)}")
    errors.extend(
        f"{name}: {audit[name]}"
        for name in (
            "english_only_definitions",
            "chinese_only_definitions",
            "missing_english_examples",
            "missing_chinese_examples",
            "orphan_examples",
            "duplicate_example_imports",
        )
        if audit.get(name)
    )
    errors.extend(
        f"{name}: {len(audit[name])}"
        for name in (
            "id_collisions",
            "parse_failures",
            "redirect_cycles",
            "unresolved_redirects",
        )
        if audit.get(name)
    )
    if include_media:
        media = audit.get("media", {})
        if media.get("missing"):
            errors.append(f"missing audio: {len(media['missing'])}")
        if media.get("stored") != EXPECTED_UNIQUE_AUDIO:
            errors.append(
                f"stored audio: expected {EXPECTED_UNIQUE_AUDIO}, got {media.get('stored')}"
            )
    return errors


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _metadata(dictionary_dir: Path, audit: dict[str, Any]) -> dict[str, Any]:
    dictionary_db = dictionary_dir / "dictionary.db"
    media_db = dictionary_dir / "media.db"
    logo = dictionary_dir / "logo.png"
    return {
        "id": "oald10",
        "name": "Oxford Advanced Learner's Dictionary 10th Bilingual",
        "version": 1,
        "entry_count": audit["entries"],
        "audio_count": audit["media"]["stored"],
        "image_count": 0,
        "files": {
            "dictionary.db": dictionary_db.stat().st_size,
            "media.db": media_db.stat().st_size,
            "logo.png": logo.stat().st_size,
        },
        "checksums": {
            "dictionary.db": file_crc32(dictionary_db),
            "media.db": file_crc32(media_db),
            "logo.png": file_crc32(logo),
        },
        "built_at": datetime.now(UTC).isoformat(),
    }


def _atomic_replace_directory(source: Path, destination: Path) -> None:
    previous = destination.with_name(f".{destination.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if destination.exists():
        destination.replace(previous)
    try:
        source.replace(destination)
    except BaseException:
        if previous.exists() and not destination.exists():
            previous.replace(destination)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def build_all(
    mdx_path: Path,
    mdd_path: Path,
    logo_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Build all generated files in a sibling temp directory and publish atomically."""

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_path.name}.build-", dir=output_path.parent))
    audit_path = temporary / "audit.json"
    audit: dict[str, Any] = {"status": "failed", "errors": ["build did not finish"]}
    try:
        entries_path = temporary / "entries.jsonl"
        LOGGER.info("converting %s", mdx_path)
        state = convert_mdx(mdx_path, entries_path)
        LOGGER.info("resolving %s redirect records", f"{len(state.redirects):,}")
        audit = state.audit
        resolve_redirects(state)
        errors = _audit_errors(audit, include_media=False)
        if errors:
            audit["status"] = "failed"
            audit["errors"] = errors
            _write_json(audit_path, audit)
            raise BuildError("; ".join(errors))

        dictionary_dir = temporary / "easydict-data" / "dictionaries" / "oald10"
        dictionary_dir.mkdir(parents=True)
        LOGGER.info("training Zstd dictionary and building dictionary.db")
        database_result = build_dictionary_db(
            entries_path,
            dictionary_dir / "dictionary.db",
            state.aliases_by_entry,
        )
        audit["database"] = database_result
        LOGGER.info("selectively importing %s word audios", f"{len(state.audio_references):,}")
        audit["media"] = import_selected_audio(
            mdd_path,
            state.audio_references,
            dictionary_dir / "media.db",
        )
        shutil.copyfile(logo_path, dictionary_dir / "logo.png")

        errors = _audit_errors(audit, include_media=True)
        if errors:
            audit["status"] = "failed"
            audit["errors"] = errors
            _write_json(audit_path, audit)
            raise BuildError("; ".join(errors))
        audit["verification"] = {
            "dictionary": verify_dictionary_db(dictionary_dir / "dictionary.db"),
            "media": verify_media_db(dictionary_dir / "media.db"),
        }
        metadata = _metadata(dictionary_dir, audit)
        _write_json(dictionary_dir / "metadata.json", metadata)
        audit["status"] = "passed"
        audit["errors"] = []
        _write_json(audit_path, audit)
        LOGGER.info("publishing completed build at %s", output_path)
        _atomic_replace_directory(temporary, output_path)
        return audit
    except BaseException:
        if not audit_path.exists():
            _write_json(audit_path, audit)
        failed_dir = output_path.parent / "artifacts"
        failed_dir.mkdir(exist_ok=True)
        shutil.copyfile(audit_path, failed_dir / "oald10-audit.failed.json")
        shutil.rmtree(temporary, ignore_errors=True)
        raise
