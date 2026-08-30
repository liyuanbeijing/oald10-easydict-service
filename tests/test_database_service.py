"""Database compatibility and five-route HTTP contract tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from oald10_easydict_service.database import (
    build_dictionary_db,
    verify_dictionary_db,
    verify_media_db,
)
from oald10_easydict_service.models import Definition, Entry, Example, Sense
from oald10_easydict_service.service import create_app


def _data_root(tmp_path: Path) -> tuple[Path, Entry, bytes]:
    data_root = tmp_path / "data"
    dictionary_dir = data_root / "dictionaries" / "oald10"
    dictionary_dir.mkdir(parents=True)
    entries: list[Entry] = []
    for index in range(1, 9):
        senses = [
            Sense(
                index="1",
                definition=Definition(
                    en=("synthetic definition " * 20) + str(index),
                    zh=("合成释义" * 20) + str(index),
                ),
                examples=[Example(en=f"Example {index}.", zh=f"例句 {index}。")],
            )
        ]
        if index == 1:
            senses.append(
                Sense(
                    index="2",
                    examples=[Example(en="Example-only sense.", zh="只有例句的义项。")],
                )
            )
        entries.append(
            Entry(
                entry_id=index,
                headword="Answer" if index == 1 else f"word-{index}",
                section="noun",
                entry_type="word",
                pos="noun",
                sense=senses,
            )
        )
    jsonl = tmp_path / "entries.jsonl"
    jsonl.write_text(
        "".join(
            entry.model_dump_json(by_alias=True, exclude_none=True) + "\n" for entry in entries
        ),
        encoding="utf-8",
    )
    build_dictionary_db(
        jsonl,
        dictionary_dir / "dictionary.db",
        {1: {"answer alias"}},
        dictionary_size=1024,
    )
    audio = b"ID3\x04\x00\x00\x00\x00\x00\x00synthetic-audio"
    media = sqlite3.connect(dictionary_dir / "media.db")
    media.execute("CREATE TABLE audios (name TEXT PRIMARY KEY, blob BLOB NOT NULL)")
    media.execute("CREATE TABLE images (name TEXT PRIMARY KEY, blob BLOB NOT NULL)")
    media.execute("INSERT INTO audios VALUES (?, ?)", ("answer.mp3", audio))
    media.execute("CREATE INDEX idx_audios_name ON audios(name)")
    media.execute("CREATE INDEX idx_images_name ON images(name)")
    media.commit()
    media.close()
    (dictionary_dir / "metadata.json").write_text(
        json.dumps({"name": "Synthetic OALD10", "version": 1}),
        encoding="utf-8",
    )
    return data_root, entries[0], audio


def test_easy_dict_schema_and_read_back(tmp_path: Path) -> None:
    data_root, _, _ = _data_root(tmp_path)
    dictionary_dir = data_root / "dictionaries" / "oald10"

    assert verify_dictionary_db(dictionary_dir / "dictionary.db") == {
        "entries": 8,
        "indices": 9,
    }
    media_result = verify_media_db(dictionary_dir / "media.db")
    assert media_result["audios"] == 1
    assert media_result["images"] == 0
    assert media_result["combined_crc32"] > 0


def test_five_route_http_contract(tmp_path: Path) -> None:
    data_root, first_entry, audio = _data_root(tmp_path)
    app = create_app(data_root)

    with TestClient(app) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "dictionary": "oald10",
        }
        dictionaries = client.get("/dictionaries").json()["dictionaries"]
        assert dictionaries[0]["id"] == "oald10"
        assert dictionaries[0]["entry_count"] == 8
        assert dictionaries[0]["audio_count"] == 1
        response = client.get("/word/oald10/answer")
        assert response.status_code == 200
        word_entry = response.json()["entries"][0]
        assert response.json()["entries"] == [
            first_entry.model_dump(mode="json", by_alias=True, exclude_none=True)
        ]
        assert word_entry["sense"][0]["examples"] == [{"en": "Example 1.", "zh": "例句 1。"}]
        assert word_entry["sense"][1]["examples"] == [
            {"en": "Example-only sense.", "zh": "只有例句的义项。"}
        ]
        assert "definition" not in word_entry["sense"][1]
        assert client.get("/word/oald10/ANSWER").json()["total"] == 1
        assert client.get("/word/oald10/missing").json()["entries"] == []
        assert client.get("/word/unknown/answer").status_code == 404
        assert client.get("/entry/oald10/not-an-id").status_code == 400
        assert client.get("/entry/oald10/0").status_code == 400
        assert client.get("/entry/oald10/9999").status_code == 404
        entry_response = client.get("/entry/oald10/1").json()
        assert entry_response == word_entry
        assert "audio/example" not in json.dumps(entry_response).casefold()
        audio_response = client.get("/audio/oald10/answer.mp3")
        assert audio_response.status_code == 200
        assert audio_response.headers["content-type"] == "audio/mpeg"
        assert int(audio_response.headers["content-length"]) == len(audio)
        assert "max-age" in audio_response.headers["cache-control"]
        assert audio_response.content.startswith(b"ID3")
        assert client.get("/audio/oald10/missing.mp3").status_code == 404
        assert client.get("/audio/oald10/..%5Csecret.mp3").status_code == 400
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        route_paths = {getattr(route, "path", None) for route in app.routes}
        assert route_paths == {
            "/health",
            "/dictionaries",
            "/word/{dict_id}/{word}",
            "/entry/{dict_id}/{entry_id}",
            "/audio/{dict_id}/{filename}",
        }


def test_health_is_503_when_data_is_unreadable(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "missing")) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"
