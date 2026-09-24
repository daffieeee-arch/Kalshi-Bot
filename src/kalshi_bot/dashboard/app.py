"""Local FastAPI app: read-only live view of the production capture."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from kalshi_bot.config import load_settings
from kalshi_bot.dashboard.feed import LiveFeed
from kalshi_bot.recorder import DEFAULT_JSONL, DEFAULT_LOCK
from kalshi_bot.session import DEFAULT_SESSION_ROOT

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    *,
    jsonl_path: Path = DEFAULT_JSONL,
    lock_path: Path = DEFAULT_LOCK,
    session_dir: Path = DEFAULT_SESSION_ROOT,
) -> FastAPI:
    settings = load_settings()
    feed = LiveFeed(
        jsonl_path=jsonl_path,
        lock_path=lock_path,
        settings=settings,
        session_dir=session_dir,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        feed.start()
        yield
        feed.stop()

    app = FastAPI(title="KXBTC15M dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.feed = feed

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state() -> dict:
        return feed.snapshot()

    @app.get("/api/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(_sse(feed), media_type="text/event-stream")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def _sse(feed: LiveFeed) -> AsyncIterator[str]:
    last = ""
    while True:
        payload = json.dumps(feed.snapshot(), separators=(",", ":"))
        if payload != last:
            yield f"data: {payload}\n\n"
            last = payload
        else:
            yield ": keepalive\n\n"
        await asyncio.sleep(0.25)
