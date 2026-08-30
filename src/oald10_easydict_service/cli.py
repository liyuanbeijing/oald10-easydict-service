"""Command-line entry points for sampling and full offline builds."""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from mdict_utils.base.readmdict import MDX
from mdict_utils.reader import get_record

from oald10_easydict_service.builder import build_all
from oald10_easydict_service.parser import parse_record

DEFAULT_WORDS = (
    "answer",
    "run",
    "record",
    "information",
    "the",
    "good",
    "set",
    "read",
    "answer back",
)


def _sample_records(mdx_path: Path, words: list[str]) -> dict[str, list[dict[str, Any]]]:
    mdx = MDX(str(mdx_path))
    wanted = {word.casefold() for word in words}
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    key_list = mdx._key_list  # noqa: SLF001 - mdict-utils has no random-access public API
    for index, (offset, raw_key) in enumerate(key_list):
        key = raw_key.decode("utf-8")
        if key.casefold() not in wanted:
            continue
        end = key_list[index + 1][0] if index + 1 < len(key_list) else offset
        content = get_record(mdx, raw_key, offset, end - offset)
        if content.strip("\x00\r\n ").startswith("@@@LINK="):
            result[key].append({"redirect": content.strip("\x00\r\n ").removeprefix("@@@LINK=")})
            continue
        parsed = parse_record(content, index + 1, key)
        result[key].extend(
            entry.model_dump(mode="json", by_alias=True, exclude_none=True)
            for entry in parsed.entries
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oald10",
        description="Build and inspect the local OALD10 EasyDict data set.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sample = subparsers.add_parser("sample", help="parse selected MDX keys")
    sample.add_argument("words", nargs="*", default=list(DEFAULT_WORDS))
    sample.add_argument(
        "--mdx",
        type=Path,
        default=Path("sources/oald10-bilingual/oald10-bilingual.mdx"),
    )
    build = subparsers.add_parser("build", help="build the complete audited data set")
    build.add_argument(
        "--mdx",
        type=Path,
        default=Path("sources/oald10-bilingual/oald10-bilingual.mdx"),
    )
    build.add_argument(
        "--mdd",
        type=Path,
        default=Path("sources/oald10-bilingual/oald10-bilingual.1.mdd"),
    )
    build.add_argument(
        "--logo",
        type=Path,
        default=Path("sources/oald10-bilingual/oald10-bilingual.png"),
    )
    build.add_argument("--output", type=Path, default=Path("generated"))
    return parser


def main() -> None:
    """Run the requested offline conversion command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    arguments = _parser().parse_args()
    if arguments.command == "sample":
        result = _sample_records(arguments.mdx, arguments.words)
        print(json.dumps(result, ensure_ascii=False, indent=2))  # noqa: T201
        return
    audit = build_all(arguments.mdx, arguments.mdd, arguments.logo, arguments.output)
    print(json.dumps(audit, ensure_ascii=False, indent=2))  # noqa: T201
