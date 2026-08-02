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
    max_concurrent INTEGER NULL,
    is_approved INTEGER DEFAULT 0,
    is_premium INTEGER DEFAULT 0,
    premium_until TEXT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    query TEXT,
    created_at TEXT,
    site TEXT DEFAULT 'akwam'
);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT,
    quality TEXT,
    status TEXT,
    created_at TEXT,
    site TEXT DEFAULT 'akwam'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
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
        # migration آمن للقواعد القديمة: إضافة أعمدة الموافقة/البريميوم لو ناقصة
        cur = await self._conn.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cur.fetchall()}
        await cur.close()
        if "is_approved" not in cols:
            await self._conn.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER DEFAULT 0")
        if "is_premium" not in cols:
            await self._conn.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
        if "premium_until" not in cols:
            await self._conn.execute("ALTER TABLE users ADD COLUMN premium_until TEXT NULL")
        # migration آمن: عمود site في requests/downloads للقواعد القديمة
        for table in ("requests", "downloads"):
            cur = await self._conn.execute(f"PRAGMA table_info({table})")
            tcols = {row[1] for row in await cur.fetchall()}
            await cur.close()
            if "site" not in tcols:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN site TEXT DEFAULT 'akwam'"
                )
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

    _USER_COLS = (
        "id, username, first_name, is_banned, max_concurrent, is_approved, "
        "is_premium, premium_until, joined_at"
    )

    # فلاتر قوائم الأعضاء (list_users/count_users)
    _USER_FILTERS = {
        "all": "",
        "premium": "WHERE is_premium = 1",
        "banned": "WHERE is_banned = 1",
        "pending": "WHERE is_approved = 0 AND is_banned = 0",
    }

    @classmethod
    def _filter_where(cls, filter: str) -> str:
        try:
            return cls._USER_FILTERS[filter]
        except KeyError:
            raise ValueError(f"فلتر أعضاء غير معروف: {filter!r}") from None

    @staticmethod
    def _user_dict(r: tuple, downloads: int = 0) -> dict:
        return {
            "id": r[0],
            "username": r[1],
            "first_name": r[2],
            "is_banned": bool(r[3]),
            "max_concurrent": r[4],
            "is_approved": bool(r[5]),
            "is_premium": bool(r[6]),
            "premium_until": r[7],
            "joined_at": r[8],
            "downloads": downloads,
        }

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

    async def get_user(self, user_id: int) -> dict | None:
        """بيانات مستخدم واحد + عدد تحميلاته، وNone لو مش موجود."""
        cur = await self.conn.execute(
            f"SELECT {self._USER_COLS}, "
            "(SELECT COUNT(*) FROM downloads WHERE downloads.user_id = users.id) "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return self._user_dict(row, downloads=row[9] or 0)

    async def is_approved(self, user_id: int) -> bool:
        val = await self._scalar("SELECT is_approved FROM users WHERE id = ?", (user_id,))
        return bool(val)

    async def set_approved(self, user_id: int, approved: bool) -> None:
        await self.conn.execute(
            "INSERT INTO users (id, is_approved) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET is_approved=excluded.is_approved",
            (user_id, int(approved)),
        )
        await self.conn.commit()

    async def is_premium(self, user_id: int) -> bool:
        """بريميوم ساري: is_premium=1 و(بدون انتهاء أو لسه في المدة)."""
        val = await self._scalar(
            "SELECT is_premium FROM users WHERE id = ? "
            "AND (premium_until IS NULL OR premium_until > datetime('now','localtime'))",
            (user_id,),
        )
        return bool(val)

    async def set_premium(self, user_id: int, premium: bool, days: int | None = None) -> None:
        """تفعيل/إلغاء البريميوم. days=None يعني دائم، غير كده بمدة بالأيام."""
        if premium and days is not None:
            await self.conn.execute(
                "INSERT INTO users (id, is_premium, premium_until) "
                "VALUES (?, 1, datetime('now','localtime', ?)) "
                "ON CONFLICT(id) DO UPDATE SET is_premium=1, "
                "premium_until=datetime('now','localtime', ?)",
                (user_id, f"+{days} days", f"+{days} days"),
            )
        else:
            await self.conn.execute(
                "INSERT INTO users (id, is_premium, premium_until) VALUES (?, ?, NULL) "
                "ON CONFLICT(id) DO UPDATE SET is_premium=excluded.is_premium, premium_until=NULL",
                (user_id, int(premium)),
            )
        await self.conn.commit()

    async def list_expired_premium(self) -> list[int]:
        """ids المستخدمين البريميوم المنتهي (بمدة وخلصت)."""
        cur = await self.conn.execute(
            "SELECT id FROM users WHERE is_premium = 1 AND premium_until IS NOT NULL "
            "AND premium_until <= datetime('now','localtime')"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [r[0] for r in rows]

    async def expire_premium(self, user_id: int) -> None:
        """إنهاء بريميوم مستخدم (بعد انتهاء المدة)."""
        await self.conn.execute(
            "UPDATE users SET is_premium = 0, premium_until = NULL WHERE id = ?",
            (user_id,),
        )
        await self.conn.commit()

    async def list_pending(self, offset: int = 0, limit: int = 10) -> list[dict]:
        """المستخدمون المعلقون: غير موافق عليهم وغير محظورين (مرفوضين)."""
        cur = await self.conn.execute(
            f"SELECT {self._USER_COLS} FROM users "
            "WHERE is_approved = 0 AND is_banned = 0 "
            "ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._user_dict(r) for r in rows]

    async def count_pending(self) -> int:
        """عدد المعلقين (غير موافق عليهم وغير محظورين)."""
        return (
            await self._scalar(
                "SELECT COUNT(*) FROM users WHERE is_approved = 0 AND is_banned = 0"
            )
            or 0
        )

    async def list_users(self, filter: str = "all", offset: int = 0, limit: int = 8) -> list[dict]:
        """قايمة الأعضاء بفلتر (all/premium/banned/pending) مع تقليب وعدد التحميلات."""
        where = self._filter_where(filter)
        cur = await self.conn.execute(
            f"SELECT {self._USER_COLS}, "
            "(SELECT COUNT(*) FROM downloads WHERE downloads.user_id = users.id) "
            f"FROM users {where} ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._user_dict(r, downloads=r[9] or 0) for r in rows]

    async def count_users(self, filter: str = "all") -> int:
        """عدد الأعضاء حسب الفلتر (all/premium/banned/pending)."""
        where = self._filter_where(filter)
        return await self._scalar(f"SELECT COUNT(*) FROM users {where}") or 0

    async def top_users(self, limit: int = 10) -> list[dict]:
        """أكثر المستخدمين تحميلًا (كل الحالات) — باستبعاد اللي عددهم 0."""
        cur = await self.conn.execute(
            "SELECT users.id, users.username, users.first_name, COUNT(downloads.id) AS dl "
            "FROM users JOIN downloads ON downloads.user_id = users.id "
            "GROUP BY users.id ORDER BY dl DESC, users.id LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            {"id": r[0], "username": r[1], "first_name": r[2], "downloads": r[3]}
            for r in rows
        ]

    async def recent_downloads(self, limit: int = 20) -> list[dict]:
        """آخر التحميلات مع بيانات المستخدم (الأحدث أولًا)."""
        cur = await self.conn.execute(
            "SELECT d.user_id, u.username, u.first_name, d.title, d.quality, d.status, "
            "d.site, d.created_at "
            "FROM downloads d LEFT JOIN users u ON u.id = d.user_id "
            "ORDER BY d.created_at DESC, d.id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            {
                "user_id": r[0],
                "username": r[1],
                "first_name": r[2],
                "title": r[3],
                "quality": r[4],
                "status": r[5],
                "site": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    async def count_new_users(self, days: int) -> int:
        """عدد الأعضاء اللي انضموا في آخر N يوم."""
        return (
            await self._scalar(
                "SELECT COUNT(*) FROM users "
                "WHERE joined_at >= datetime('now','localtime', '-' || ? || ' days')",
                (days,),
            )
            or 0
        )

    async def top_titles(self, limit: int = 10) -> list[tuple[str, int]]:
        """أكثر العناوين تحميلًا ناجحًا (عنوان، عدد)."""
        cur = await self.conn.execute(
            "SELECT title, COUNT(*) FROM downloads WHERE status = 'done' "
            "GROUP BY title ORDER BY COUNT(*) DESC, title LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [(r[0], r[1]) for r in rows]

    async def log_request(self, user_id: int, query: str, site: str = "akwam") -> None:
        await self.conn.execute(
            "INSERT INTO requests (user_id, query, created_at, site) "
            "VALUES (?, ?, datetime('now','localtime'), ?)",
            (user_id, query, site),
        )
        await self.conn.commit()

    async def log_download(
        self, user_id: int, title: str, quality: str, status: str, site: str = "akwam"
    ) -> None:
        await self.conn.execute(
            "INSERT INTO downloads (user_id, title, quality, status, created_at, site) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'), ?)",
            (user_id, title, quality, status, site),
        )
        await self.conn.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        val = await self._scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return val if val is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
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
            "premium": await self._scalar("SELECT COUNT(*) FROM users WHERE is_premium = 1") or 0,
            "pending": await self._scalar(
                "SELECT COUNT(*) FROM users WHERE is_approved = 0 AND is_banned = 0"
            )
            or 0,
            "requests_akwam": await self._scalar(
                "SELECT COUNT(*) FROM requests WHERE site = 'akwam'"
            )
            or 0,
            "requests_starcima": await self._scalar(
                "SELECT COUNT(*) FROM requests WHERE site = 'starcima'"
            )
            or 0,
            "downloads_akwam": await self._scalar(
                "SELECT COUNT(*) FROM downloads WHERE site = 'akwam'"
            )
            or 0,
            "downloads_starcima": await self._scalar(
                "SELECT COUNT(*) FROM downloads WHERE site = 'starcima'"
            )
            or 0,
        }

    async def all_user_ids(self, audience: str = "all") -> list[int]:
        """ids للإذاعة: all (الكل) / premium (ساري) / free (مش بريميوم أو منتهي)."""
        active_prem = (
            "is_premium = 1 AND (premium_until IS NULL "
            "OR premium_until > datetime('now','localtime'))"
        )
        if audience == "premium":
            sql = f"SELECT id FROM users WHERE {active_prem}"
        elif audience == "free":
            sql = f"SELECT id FROM users WHERE NOT ({active_prem})"
        elif audience == "all":
            sql = "SELECT id FROM users"
        else:
            raise ValueError(f"جمهور إذاعة غير معروف: {audience!r}")
        cur = await self.conn.execute(sql)
        rows = await cur.fetchall()
        await cur.close()
        return [r[0] for r in rows]

    async def search_users(self, query: str) -> list[dict]:
        """بحث بالـ id أو username."""
        q = query.strip().lstrip("@")
        dl_count = "(SELECT COUNT(*) FROM downloads WHERE downloads.user_id = users.id)"
        if q.isdigit():
            cur = await self.conn.execute(
                f"SELECT {self._USER_COLS}, {dl_count} FROM users WHERE id = ?",
                (int(q),),
            )
        else:
            cur = await self.conn.execute(
                f"SELECT {self._USER_COLS}, {dl_count} FROM users "
                "WHERE username LIKE ? OR first_name LIKE ? LIMIT 10",
                (f"%{q}%", f"%{q}%"),
            )
        rows = await cur.fetchall()
        await cur.close()
        return [self._user_dict(r, downloads=r[9] or 0) for r in rows]

    async def list_banned(self) -> list[dict]:
        cur = await self.conn.execute(
            f"SELECT {self._USER_COLS} FROM users WHERE is_banned = 1"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._user_dict(r) for r in rows]
