from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    author TEXT NOT NULL DEFAULT '',
    comment_text TEXT NOT NULL,
    publication_url TEXT NOT NULL DEFAULT '',
    publication_title TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    ai_reply TEXT NOT NULL DEFAULT '',
    final_reply TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    replied_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_updated ON comments(updated_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _init_sync(self) -> None:
        with self._connect() as con:
            con.executescript(SCHEMA)

    async def is_processed(self, comment_id: str) -> bool:
        return await asyncio.to_thread(self._is_processed_sync, comment_id)

    def _is_processed_sync(self, comment_id: str) -> bool:
        with self._connect() as con:
            row = con.execute("SELECT action, published FROM comments WHERE comment_id=?", (comment_id,)).fetchone()
            if not row:
                return False
            # review/error entries may be retried manually; reply/skip are final.
            return row["action"] in {"reply", "skip"} and (row["action"] == "skip" or bool(row["published"]))

    async def save_result(
        self,
        *,
        comment_id: str,
        author: str,
        comment_text: str,
        publication_url: str,
        publication_title: str,
        action: str,
        ai_reply: str = "",
        final_reply: str = "",
        reason: str = "",
        published: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self._save_result_sync,
            comment_id,
            author,
            comment_text,
            publication_url,
            publication_title,
            action,
            ai_reply,
            final_reply,
            reason,
            published,
        )

    def _save_result_sync(self, *args: Any) -> None:
        (
            comment_id,
            author,
            comment_text,
            publication_url,
            publication_title,
            action,
            ai_reply,
            final_reply,
            reason,
            published,
        ) = args
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO comments (
                    comment_id, author, comment_text, publication_url, publication_title,
                    action, ai_reply, final_reply, reason, published, attempts, replied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
                ON CONFLICT(comment_id) DO UPDATE SET
                    author=excluded.author,
                    comment_text=excluded.comment_text,
                    publication_url=excluded.publication_url,
                    publication_title=excluded.publication_title,
                    action=excluded.action,
                    ai_reply=excluded.ai_reply,
                    final_reply=excluded.final_reply,
                    reason=excluded.reason,
                    published=excluded.published,
                    attempts=comments.attempts + 1,
                    updated_at=CURRENT_TIMESTAMP,
                    replied_at=CASE WHEN excluded.published THEN CURRENT_TIMESTAMP ELSE comments.replied_at END
                """,
                (
                    comment_id,
                    author,
                    comment_text,
                    publication_url,
                    publication_title,
                    action,
                    ai_reply,
                    final_reply,
                    reason,
                    int(published),
                    int(published),
                ),
            )

    async def get_setting(self, key: str, default: str = "") -> str:
        return await asyncio.to_thread(self._get_setting_sync, key, default)

    def _get_setting_sync(self, key: str, default: str) -> str:
        with self._connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set_setting_sync, key, value)

    def _set_setting_sync(self, key: str, value: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    async def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM comments ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, int]:
        with self._connect() as con:
            total = con.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            replied = con.execute("SELECT COUNT(*) FROM comments WHERE published=1").fetchone()[0]
            skipped = con.execute("SELECT COUNT(*) FROM comments WHERE action='skip'").fetchone()[0]
            review = con.execute("SELECT COUNT(*) FROM comments WHERE action='review'").fetchone()[0]
            return {"total": total, "replied": replied, "skipped": skipped, "review": review}
