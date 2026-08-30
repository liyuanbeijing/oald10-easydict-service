"""Validation tests for 20 sampled dictionary words against generated EasyDict data."""

from __future__ import annotations

import urllib.parse
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oald10_easydict_service.models import Entry, Sense, SenseGroup
from oald10_easydict_service.service import create_app

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sampled_words.txt"
DATA_ROOT = Path("generated/easydict-data")


def _load_sampled_words() -> list[str]:
    if not FIXTURE_PATH.is_file():
        return []
    return [
        line.strip()
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


SAMPLED_WORDS = _load_sampled_words()


@pytest.fixture(scope="module")
def api_client() -> Generator[TestClient, None, None]:
    """Provide a TestClient instance pointing at the generated dictionary data."""
    app = create_app(DATA_ROOT)
    with TestClient(app) as client:
        yield client


def _check_sense_nodes(nodes: list[Sense | SenseGroup], headword: str) -> None:
    for node in nodes:
        if isinstance(node, SenseGroup):
            assert node.title.en
            assert node.title.zh
            _check_sense_nodes(node.senses, headword)
        elif isinstance(node, Sense):
            if node.definition is not None:
                assert node.definition.en, f"Missing English definition in {headword}"
                assert node.definition.zh, f"Missing Chinese definition in {headword}"
            for ex in node.examples:
                assert ex.en, f"Missing English example text in {headword}"
                assert ex.zh, f"Missing Chinese example text in {headword}"


def test_sampled_words_fixture_count() -> None:
    """Verify that the fixture file contains exactly 20 distinct words."""
    assert len(SAMPLED_WORDS) == 20
    assert len(set(SAMPLED_WORDS)) == 20


@pytest.mark.skipif(
    not (DATA_ROOT / "dictionaries" / "oald10" / "dictionary.db").exists(),
    reason="generated dictionary database is absent",
)
@pytest.mark.parametrize("word", SAMPLED_WORDS)
def test_sampled_word_query_and_contract(word: str, api_client: TestClient) -> None:
    """Verify that querying each sampled word returns valid entries, definitions, and audio."""
    encoded_word = urllib.parse.quote(word)
    response = api_client.get(f"/word/oald10/{encoded_word}")
    assert response.status_code == 200, f"Query failed for word {word!r}: {response.text}"

    data = response.json()
    assert data["dict_id"] == "oald10"
    assert data["word"] == word
    assert isinstance(data["entries"], list)
    assert data["total"] == len(data["entries"])
    assert data["total"] >= 1, f"Expected at least one entry for {word!r}"

    for raw_entry in data["entries"]:
        entry = Entry.model_validate(raw_entry)
        assert entry.dict_id == "oald10"
        assert entry.entry_id > 0
        assert entry.headword
        assert entry.pos

        # Verify direct entry lookup by ID matches the word payload entry
        entry_response = api_client.get(f"/entry/oald10/{entry.entry_id}")
        assert entry_response.status_code == 200
        assert entry_response.json() == raw_entry

        # Verify senses, definitions, and example bilingual integrity
        _check_sense_nodes(entry.sense, entry.headword)

        # Verify all associated pronunciations and audio endpoints
        for pron in entry.pronunciation:
            assert pron.notation
            if pron.audio_url:
                audio_response = api_client.get(pron.audio_url)
                assert audio_response.status_code == 200, (
                    f"Missing audio for {pron.audio_url} in {entry.headword}"
                )
                assert audio_response.headers["content-type"] == "audio/mpeg"
                assert len(audio_response.content) > 0
