"""طبقة التخزين — SQLite عبر aiosqlite (حسب SPEC 3.4)."""
from __future__ import annotations

import os
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined_at TEXT DEFAULT (datetime('now','localtime')),
    is_banned INTEGER DEFAULT 0,
    max_concurrent INTEGER NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    query TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT,
    quality TEXT,
    status TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """ينشئ مجلد قاعدة البيانات والجداول لو مش موجودة."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() لسه مااتنفذتش")
        return self._conn

    async def _scalar(self, sql: str, params: tuple = ()) -> Any:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO users (id, username, first_name) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user_id, username, first_name),
        )
        await self.conn.commit()

    async def is_banned(self, user_id: int) -> bool:
        val = await self._scalar("SELECT is_banned FROM users WHERE id = ?", (user_id,))
        return bool(val)

    async def set_ban(self, user_id: int, banned: bool) -> None:
        await self.conn.execute(
            "INSERT INTO users (id, is_banned) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET is_banned=excluded.is_banned",
            (user_id, int(banned)),
        )
        await self.conn.commit()

    async def get_user_limit(self, user_id: int) -> int | None:
        """None = استخدم الافتراضي."""
        return await self._scalar("SELECT max_concurrent FROM users WHERE id = ?", (user_id,))

    async def set_user_limit(self, user_id: int, limit: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO users (id, max_concurrent) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET max_concurrent=excluded.max_concurrent",
            (user_id, limit),
        )
        await self.conn.commit()

    async def log_request(self, user_id: int, query: str) -> None:
        await self.conn.execute(
            "INSERT INTO requests (user_id, query, created_at) VALUES (?, ?, datetime('now','localtime'))",
            (user_id, query),
        )
        await self.conn.commit()

    async def log_download(self, user_id: int, title: str, quality: str, status: str) -> None:
        await self.conn.execute(
            "INSERT INTO downloads (user_id, title, quality, status, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            (user_id, title, quality, status),
        )
        await self.conn.commit()

    async def stats(self) -> dict:
        return {
            "users": await self._scalar("SELECT COUNT(*) FROM users") or 0,
            "banned": await self._scalar("SELECT COUNT(*) FROM users WHERE is_banned = 1") or 0,
            "requests": await self._scalar("SELECT COUNT(*) FROM requests") or 0,
            "downloads_done": await self._scalar("SELECT COUNT(*) FROM downloads WHERE status = 'done'") or 0,
            "downloads_today": await self._scalar(
                "SELECT COUNT(*) FROM downloads WHERE date(created_at) = date('now','localtime')"
            )
            or 0,
        }

    async def all_user_ids(self) -> list[int]:
        cur = await self.conn.execute("SELECT id FROM users")
        rows = await cur.fetchall()
        await cur.close()
        return [r[0] for r in rows]

    async def search_users(self, query: str) -> list[dict]:
        """بحث بالـ id أو username."""
        q = query.strip().lstrip("@")
        if q.isdigit():
            cur = await self.conn.execute(
                "SELECT id, username, first_name, is_banned, max_concurrent FROM users WHERE id = ?",
                (int(q),),
            )
        else:
            cur = await self.conn.execute(
                "SELECT id, username, first_name, is_banned, max_concurrent FROM users "
                "WHERE username LIKE ? OR first_name LIKE ? LIMIT 10",
                (f"%{q}%", f"%{q}%"),
            )
        rows = await cur.fetchall()
        await cur.close()
        return [
            {
                "id": r[0],
                "username": r[1],
                "first_name": r[2],
                "is_banned": bool(r[3]),
                "max_concurrent": r[4],
            }
            for r in rows
        ]

    async def list_banned(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT id, username, first_name, is_banned, max_concurrent FROM users WHERE is_banned = 1"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            {
                "id": r[0],
                "username": r[1],
                "first_name": r[2],
                "is_banned": bool(r[3]),
                "max_concurrent": r[4],
            }
            for r in rows
        ]
