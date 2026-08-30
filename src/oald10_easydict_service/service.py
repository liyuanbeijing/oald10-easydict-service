"""The five-route, read-only FastAPI application."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePath

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from oald10_easydict_service.store import DictionaryStore, StoreUnavailableError

DICTIONARY_ID = "oald10"
MAX_ENTRY_ID = 2**31 - 1
_DECIMAL_ID = re.compile(r"[0-9]+\Z")


def _safe_audio_filename(filename: str) -> bool:
    return (
        bool(filename)
        and PurePath(filename).name == filename
        and "/" not in filename
        and "\\" not in filename
        and "\x00" not in filename
        and filename not in {".", ".."}
        and filename.casefold().endswith(".mp3")
    )


def create_app(  # noqa: C901 - route handlers remain colocated with their lifespan dependency
    data_path: Path | None = None,
) -> FastAPI:
    """Create the service, optionally pointing tests at a temporary data root."""

    configured_path = data_path or Path(
        os.environ.get("OALD10_DATA_PATH", "generated/easydict-data")
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        store = DictionaryStore(configured_path)
        try:
            store.open()
        except StoreUnavailableError as error:
            application.state.store = None
            application.state.store_error = str(error)
        else:
            application.state.store = store
            application.state.store_error = None
        try:
            yield
        finally:
            store.close()

    application = FastAPI(
        title="OALD10 EasyDict Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )

    def require_dictionary(dict_id: str) -> DictionaryStore:
        if dict_id != DICTIONARY_ID:
            raise HTTPException(status_code=404, detail=f"Dictionary '{dict_id}' not found")
        store: DictionaryStore | None = application.state.store
        if store is None:
            raise HTTPException(status_code=503, detail="Dictionary data is unavailable")
        return store

    @application.get("/health")
    def health() -> JSONResponse:
        store: DictionaryStore | None = application.state.store
        if store is None:
            return JSONResponse(
                {"status": "unavailable", "dictionary": DICTIONARY_ID},
                status_code=503,
            )
        try:
            store.ping()
        except (StoreUnavailableError, OSError):
            return JSONResponse(
                {"status": "unavailable", "dictionary": DICTIONARY_ID},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "dictionary": DICTIONARY_ID})

    @application.get("/dictionaries")
    def dictionaries() -> dict[str, list[dict[str, object]]]:
        store = require_dictionary(DICTIONARY_ID)
        return {"dictionaries": [store.info()]}

    @application.get("/word/{dict_id}/{word}")
    def word(dict_id: str, word: str) -> dict[str, object]:
        store = require_dictionary(dict_id)
        entries = store.query_word(word)
        return {
            "dict_id": dict_id,
            "word": word,
            "entries": entries,
            "total": len(entries),
        }

    @application.get("/entry/{dict_id}/{entry_id}")
    def entry(dict_id: str, entry_id: str) -> dict[str, object]:
        store = require_dictionary(dict_id)
        if not _DECIMAL_ID.fullmatch(entry_id):
            raise HTTPException(status_code=400, detail="Entry ID must be a positive integer")
        numeric_id = int(entry_id)
        if numeric_id <= 0 or numeric_id > MAX_ENTRY_ID:
            raise HTTPException(status_code=400, detail="Entry ID is outside the valid range")
        result = store.query_entry(numeric_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Entry '{numeric_id}' not found")
        return result

    @application.get("/audio/{dict_id}/{filename}")
    def audio(dict_id: str, filename: str) -> Response:
        store = require_dictionary(dict_id)
        if not _safe_audio_filename(filename):
            raise HTTPException(status_code=400, detail="Unsafe MP3 filename")
        content = store.query_audio(filename)
        if content is None:
            raise HTTPException(status_code=404, detail=f"Audio '{filename}' not found")
        return Response(
            content=content,
            media_type="audio/mpeg",
            headers={"Cache-Control": "public, max-age=2592000, immutable"},
        )

    return application


app = create_app()
