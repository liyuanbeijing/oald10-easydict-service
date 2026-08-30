"""Build orchestration, redirect, selective MDD, and CLI tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from oald10_easydict_service import builder, cli
from oald10_easydict_service.builder import BuildError, ConversionState
from oald10_easydict_service.mdict_source import (
    MDXRecord,
    import_selected_audio,
    iter_mdx_records,
    mdx_record_count,
)

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_entry.html"
LOCAL_MDX = Path("sources/oald10-bilingual/oald10-bilingual.mdx")
LOCAL_MDD = Path("sources/oald10-bilingual/oald10-bilingual.1.mdd")


def test_conversion_and_redirect_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversion streams entries and classifies aliases, cycles, and failures."""
    records = iter(
        [
            MDXRecord(1, "sample", FIXTURE.read_text(encoding="utf-8")),
            MDXRecord(2, "alias", "@@@LINK=sample\n\x00"),
            MDXRecord(3, "chain", "@@@LINK=alias\n\x00"),
            MDXRecord(4, "cycle-a", "@@@LINK=cycle-b\n\x00"),
            MDXRecord(5, "cycle-b", "@@@LINK=cycle-a\n\x00"),
            MDXRecord(6, "broken", "<html>not an entry</html>"),
        ]
    )
    monkeypatch.setattr(builder, "iter_mdx_records", lambda _path: records)
    jsonl = tmp_path / "entries.jsonl"

    state = builder.convert_mdx(tmp_path / "source.mdx", jsonl)
    builder.resolve_redirects(state)

    assert state.audit["mdx_records"] == 6
    assert state.audit["redirects"] == 4
    assert state.audit["entries"] == 2
    assert len(state.audit["parse_failures"]) == 1
    assert state.audit["raw_examples"] == 9
    assert state.audit["retained_examples"] == 7
    assert state.audit["retained_example_lists"] == 6
    assert state.audit["excluded_examples"] == 2
    assert state.audit["missing_english_examples"] == 0
    assert state.audit["missing_chinese_examples"] == 0
    assert state.audit["orphan_examples"] == 0
    assert state.audit["duplicate_example_imports"] == 0
    assert state.audit["redirect_cycles"]
    assert len(state.audit["unresolved_redirects"]) == 2
    noun_id = (1 << 10) | 1
    assert {"sample", "alias", "chain"}.issubset(state.aliases_by_entry[noun_id])
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 2


def _successful_state() -> ConversionState:
    state = ConversionState()
    state.audit.update(
        {
            "mdx_records": builder.EXPECTED_MDX_RECORDS,
            "redirects": builder.EXPECTED_REDIRECTS,
            "unique_audio_references": builder.EXPECTED_UNIQUE_AUDIO,
            "raw_examples": builder.EXPECTED_RAW_EXAMPLES,
            "retained_examples": builder.EXPECTED_RETAINED_EXAMPLES,
            "retained_example_lists": builder.EXPECTED_RETAINED_EXAMPLE_LISTS,
            "excluded_examples": builder.EXPECTED_EXCLUDED_EXAMPLES,
            "entries": 8,
        }
    )
    return state


def test_atomic_build_orchestration_and_failed_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful build publishes four files; a bad audit remains recoverable."""
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"PNG")

    def fake_convert(_mdx: Path, jsonl: Path) -> ConversionState:
        jsonl.write_text("{}\n", encoding="utf-8")
        return _successful_state()

    def fake_dictionary(
        _jsonl: Path,
        path: Path,
        _aliases: dict[int, set[str]],
    ) -> dict[str, int]:
        path.write_bytes(b"dictionary")
        return {"entries": 8, "indices": 9}

    def fake_media(
        _mdd: Path,
        _filenames: set[str],
        path: Path,
    ) -> dict[str, int | list[str]]:
        path.write_bytes(b"media")
        return {
            "requested": builder.EXPECTED_UNIQUE_AUDIO,
            "stored": builder.EXPECTED_UNIQUE_AUDIO,
            "decoded_blocks": 1,
            "missing": [],
            "duplicate_keys": [],
        }

    monkeypatch.setattr(builder, "convert_mdx", fake_convert)
    monkeypatch.setattr(builder, "build_dictionary_db", fake_dictionary)
    monkeypatch.setattr(builder, "import_selected_audio", fake_media)
    monkeypatch.setattr(builder, "verify_dictionary_db", lambda _path: {"entries": 8})
    monkeypatch.setattr(builder, "verify_media_db", lambda _path: {"audios": 1})
    output = tmp_path / "generated"

    audit = builder.build_all(tmp_path / "source.mdx", tmp_path / "source.mdd", logo, output)
    assert audit["status"] == "passed"
    dictionary_dir = output / "easydict-data" / "dictionaries" / "oald10"
    assert {path.name for path in dictionary_dir.iterdir()} == {
        "dictionary.db",
        "media.db",
        "metadata.json",
        "logo.png",
    }
    assert json.loads((output / "audit.json").read_text())["status"] == "passed"
    builder.build_all(tmp_path / "source.mdx", tmp_path / "source.mdd", logo, output)

    def bad_convert(_mdx: Path, jsonl: Path) -> ConversionState:
        jsonl.write_text("", encoding="utf-8")
        return ConversionState()

    monkeypatch.setattr(builder, "convert_mdx", bad_convert)
    with pytest.raises(BuildError):
        builder.build_all(
            tmp_path / "source.mdx",
            tmp_path / "source.mdd",
            logo,
            tmp_path / "bad-output",
        )
    failed_audit = tmp_path / "artifacts" / "oald10-audit.failed.json"
    assert json.loads(failed_audit.read_text())["status"] == "failed"


@pytest.mark.skipif(
    not (LOCAL_MDX.exists() and LOCAL_MDD.exists()),
    reason="local copyrighted source is absent",
)
def test_local_mdict_stream_and_selective_audio(tmp_path: Path) -> None:
    """The local source count and selective block extraction match known samples."""
    assert mdx_record_count(LOCAL_MDX) == builder.EXPECTED_MDX_RECORDS
    records = iter_mdx_records(LOCAL_MDX)
    first = next(records)
    records.close()
    assert first.number == 1
    assert first.key
    media_path = tmp_path / "media.db"
    result = import_selected_audio(
        LOCAL_MDD,
        {"answer__gb_1.mp3", "answer__us_1.mp3"},
        media_path,
    )
    assert result["requested"] == 2
    assert result["stored"] == 2
    assert result["missing"] == []


@pytest.mark.skipif(not LOCAL_MDX.exists(), reason="local copyrighted source is absent")
def test_cli_sample_and_build_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both public CLI subcommands dispatch and emit JSON."""
    monkeypatch.setattr(sys, "argv", ["oald10", "sample", "answer"])
    cli.main()
    sampled = json.loads(capsys.readouterr().out)
    assert any(item.get("pos") == "noun" for item in sampled["answer"])
    monkeypatch.setattr(cli, "build_all", lambda *_args: {"status": "passed"})
    monkeypatch.setattr(sys, "argv", ["oald10", "build"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "passed"}
