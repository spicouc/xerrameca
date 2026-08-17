from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .config import settings
from .persistence.schema import SCHEMA_SQL


def _db_path(path: str | None = None) -> str:
    return path or settings.XERRAMECA_DB_PATH


@asynccontextmanager
async def get_db(path: str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    target = _db_path(path)
    if target != ":memory:":
        Path(target).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(target)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 5000")
    try:
        yield db
    finally:
        await db.close()


async def init_db(path: str | None = None) -> None:
    async with get_db(path) as db:
        await db.executescript(SCHEMA_SQL)
        cursor = await db.execute("PRAGMA quick_check")
        row = await cursor.fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {row[0] if row else 'no result'}")
        await db.commit()
