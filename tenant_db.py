from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Any

import aiosqlite

DB_PATH: Path | None = None
_DB_LOCK = asyncio.Lock()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat(timespec="seconds")


def _path() -> Path:
    if DB_PATH is None:
        raise RuntimeError("Tenant БД не инициализирована")
    return DB_PATH


def _topic_key(title: str) -> str:
    return " ".join(title.split()).casefold()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def init_db(path: Path) -> None:
    global DB_PATH
    DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS app_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'inactive',
                starts_at TEXT,
                expires_at TEXT,
                source TEXT,
                stars_amount INTEGER NOT NULL DEFAULT 0,
                telegram_payment_charge_id TEXT,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                currency TEXT NOT NULL,
                total_amount INTEGER NOT NULL,
                telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                provider_payment_charge_id TEXT,
                expires_at TEXT,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                is_first_recurring INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tenant_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                username TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tenant_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                title_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                pinned INTEGER NOT NULL DEFAULT 0,
                reserved INTEGER NOT NULL DEFAULT 0,
                used_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, title_key),
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tenant_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                publish_time TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_id, publish_time),
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tenant_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(user_id, key),
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tenant_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                topic_id INTEGER,
                topic_title TEXT NOT NULL,
                article_title TEXT NOT NULL,
                article_body TEXT NOT NULL,
                image_path TEXT,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                channels_published INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tenant_topics_user_active
                ON tenant_topics(user_id, active, reserved);
            CREATE INDEX IF NOT EXISTS idx_tenant_schedule_user_time
                ON tenant_schedule(user_id, publish_time, enabled);
            CREATE INDEX IF NOT EXISTS idx_tenant_publications_user_created
                ON tenant_publications(user_id, created_at DESC);
            """
        )

        # v50 migration: закреплённые темы. Для существующих БД колонка
        # добавляется без удаления старых тем/истории. Старые темы остаются
        # незакреплёнными, пока пользователь сам их не включит в автопул.
        cur = await conn.execute("PRAGMA table_info(tenant_topics)")
        columns = {str(row[1]) for row in await cur.fetchall()}
        if "pinned" not in columns:
            await conn.execute(
                "ALTER TABLE tenant_topics ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenant_topics_user_pinned "
            "ON tenant_topics(user_id, active, pinned, reserved)"
        )
        await conn.commit()


async def _ensure_user_row(user_id: int) -> None:
    now = _now()
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            """INSERT OR IGNORE INTO app_users
               (user_id, created_at, updated_at) VALUES (?, ?, ?)""",
            (user_id, now, now),
        )
        await conn.commit()


async def touch_user(user: Any) -> None:
    if user is None or getattr(user, "id", None) is None:
        return
    user_id = int(user.id)
    now = _now()
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            """
            INSERT INTO app_users(user_id, username, first_name, last_name, language_code, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                language_code=excluded.language_code,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                getattr(user, "language_code", None),
                now,
                now,
            ),
        )
        await conn.commit()


async def subscription_info(user_id: int):
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,))
        return await cur.fetchone()


async def is_subscription_active(user_id: int) -> bool:
    row = await subscription_info(user_id)
    if not row or row["status"] != "active":
        return False
    expires = _parse_dt(row["expires_at"])
    return bool(expires and expires > _now_dt())


async def activate_subscription(
    user_id: int,
    *,
    expires_at: datetime,
    source: str,
    stars_amount: int = 0,
    telegram_payment_charge_id: str | None = None,
    is_recurring: bool = False,
) -> None:
    await _ensure_user_row(user_id)
    now = _now()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expiry = expires_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            """
            INSERT INTO subscriptions(
                user_id,status,starts_at,expires_at,source,stars_amount,
                telegram_payment_charge_id,is_recurring,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                status='active',
                starts_at=COALESCE(subscriptions.starts_at, excluded.starts_at),
                expires_at=excluded.expires_at,
                source=excluded.source,
                stars_amount=excluded.stars_amount,
                telegram_payment_charge_id=excluded.telegram_payment_charge_id,
                is_recurring=excluded.is_recurring,
                updated_at=excluded.updated_at
            """,
            (
                user_id, "active", now, expiry, source, int(stars_amount or 0),
                telegram_payment_charge_id, int(bool(is_recurring)), now,
            ),
        )
        await conn.commit()


async def grant_subscription(user_id: int, days: int = 30) -> datetime:
    await _ensure_user_row(user_id)
    current = await subscription_info(user_id)
    base = _now_dt()
    if current:
        old = _parse_dt(current["expires_at"])
        if old and old > base:
            base = old
    expires = base + timedelta(days=max(1, days))
    await activate_subscription(user_id, expires_at=expires, source="manual_grant")
    return expires


async def revoke_subscription(user_id: int) -> None:
    await _ensure_user_row(user_id)
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            """
            INSERT INTO subscriptions(user_id,status,updated_at)
            VALUES(?, 'revoked', ?)
            ON CONFLICT(user_id) DO UPDATE SET status='revoked', updated_at=excluded.updated_at
            """,
            (user_id, _now()),
        )
        await conn.commit()


async def record_payment(
    *,
    user_id: int,
    payload: str,
    currency: str,
    total_amount: int,
    telegram_payment_charge_id: str,
    provider_payment_charge_id: str | None,
    expires_at: datetime | None,
    is_recurring: bool,
    is_first_recurring: bool,
) -> None:
    await _ensure_user_row(user_id)
    expiry = None
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expiry = expires_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO payments(
                user_id,payload,currency,total_amount,telegram_payment_charge_id,
                provider_payment_charge_id,expires_at,is_recurring,is_first_recurring,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id, payload, currency, total_amount, telegram_payment_charge_id,
                provider_payment_charge_id, expiry, int(bool(is_recurring)),
                int(bool(is_first_recurring)), _now(),
            ),
        )
        await conn.commit()


async def ensure_defaults(user_id: int, publish_times: Iterable[str], topics: Iterable[str]) -> None:
    await _ensure_user_row(user_id)
    now = _now()
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO tenant_settings(user_id,key,value) VALUES(?,?,?)",
            (user_id, "auto_publish_enabled", "1"),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO tenant_settings(user_id,key,value) VALUES(?,?,?)",
            (user_id, "last_auto_slot", ""),
        )
        for value in publish_times:
            value = str(value).strip()
            if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                await conn.execute(
                    "INSERT OR IGNORE INTO tenant_schedule(user_id,publish_time,enabled) VALUES(?,?,1)",
                    (user_id, value),
                )
        for raw in topics:
            title = " ".join(str(raw).split())
            if not title:
                continue
            await conn.execute(
                """
                INSERT OR IGNORE INTO tenant_topics(user_id,title,title_key,created_at)
                VALUES(?,?,?,?)
                """,
                (user_id, title, _topic_key(title), now),
            )
        await conn.commit()


async def get_setting(user_id: int, key: str, default: str = "") -> str:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "SELECT value FROM tenant_settings WHERE user_id=? AND key=?", (user_id, key)
        )
        row = await cur.fetchone()
        return str(row[0]) if row else default


async def set_setting(user_id: int, key: str, value: str) -> None:
    await _ensure_user_row(user_id)
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            """
            INSERT INTO tenant_settings(user_id,key,value) VALUES(?,?,?)
            ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value
            """,
            (user_id, key, value),
        )
        await conn.commit()


async def auto_publish_enabled(user_id: int) -> bool:
    return (await get_setting(user_id, "auto_publish_enabled", "1")) == "1"


async def set_auto_publish_enabled(user_id: int, enabled: bool) -> None:
    await set_setting(user_id, "auto_publish_enabled", "1" if enabled else "0")


async def connect_channel(user_id: int, *, chat_id: int, title: str, username: str | None) -> str:
    await _ensure_user_row(user_id)
    now = _now()
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT user_id FROM tenant_channels WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        if row and int(row["user_id"]) != user_id:
            return "owned_by_other"
        if row:
            await conn.execute(
                "UPDATE tenant_channels SET title=?,username=?,active=1,updated_at=? WHERE chat_id=? AND user_id=?",
                (title, username, now, chat_id, user_id),
            )
        else:
            await conn.execute(
                """INSERT INTO tenant_channels(user_id,chat_id,title,username,active,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?)""",
                (user_id, chat_id, title, username, now, now),
            )
        await conn.commit()
    return "ok"


async def list_channels(user_id: int):
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tenant_channels WHERE user_id=? AND active=1 ORDER BY id", (user_id,)
        )
        return await cur.fetchall()


async def channel_count(user_id: int) -> int:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM tenant_channels WHERE user_id=? AND active=1", (user_id,)
        )
        return int((await cur.fetchone())[0])


async def remove_channel(user_id: int, channel_id: int) -> bool:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "UPDATE tenant_channels SET active=0,updated_at=? WHERE user_id=? AND id=? AND active=1",
            (_now(), user_id, channel_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def add_topics(user_id: int, titles: Iterable[str], *, pin: bool = True) -> int:
    """Добавляет несколько тем. Новые темы по умолчанию сразу закрепляются.

    Если тема уже существует, она снова становится активной; при pin=True также
    включается в постоянный автопул. Возвращается число реально новых тем.
    """
    await _ensure_user_row(user_id)
    count = 0
    pinned_value = 1 if pin else 0
    async with aiosqlite.connect(_path()) as conn:
        for raw in titles:
            title = " ".join(str(raw).split())
            if not title:
                continue
            key = _topic_key(title)
            cur = await conn.execute(
                """INSERT OR IGNORE INTO tenant_topics
                   (user_id,title,title_key,active,pinned,created_at)
                   VALUES(?,?,?,1,?,?)""",
                (user_id, title, key, pinned_value, _now()),
            )
            if cur.rowcount:
                count += 1
            else:
                # Тема могла быть добавлена раньше или удалена из активного списка.
                await conn.execute(
                    """UPDATE tenant_topics
                       SET active=1, pinned=CASE WHEN ?=1 THEN 1 ELSE pinned END
                       WHERE user_id=? AND title_key=?""",
                    (pinned_value, user_id, key),
                )
        await conn.commit()
    return count


async def list_topics(user_id: int, mode: str = "all", limit: int = 100):
    conditions = ["user_id=?", "active=1"]
    params: list[Any] = [user_id]
    if mode == "unused":
        conditions.append("used_at IS NULL")
    elif mode == "used":
        conditions.append("used_at IS NOT NULL")
    elif mode == "pinned":
        conditions.append("pinned=1")
    elif mode == "unpinned":
        conditions.append("pinned=0")
    sql = f"""
        SELECT id,title,active,pinned,reserved,used_at,created_at
        FROM tenant_topics
        WHERE {' AND '.join(conditions)}
        ORDER BY pinned DESC,
                 CASE WHEN used_at IS NULL THEN 0 ELSE 1 END,
                 COALESCE(used_at,created_at) DESC, id DESC
        LIMIT ?
    """
    params.append(limit)
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def get_topic(user_id: int, topic_id: int):
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tenant_topics WHERE user_id=? AND id=?", (user_id, topic_id)
        )
        return await cur.fetchone()


async def update_topic(user_id: int, topic_id: int, new_title: str) -> bool:
    title = " ".join(new_title.split())
    if not title:
        return False
    try:
        async with aiosqlite.connect(_path()) as conn:
            cur = await conn.execute(
                "UPDATE tenant_topics SET title=?,title_key=? WHERE user_id=? AND id=?",
                (title, _topic_key(title), user_id, topic_id),
            )
            await conn.commit()
            return cur.rowcount > 0
    except aiosqlite.IntegrityError:
        return False


async def deactivate_topic(user_id: int, topic_id: int) -> bool:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "UPDATE tenant_topics SET active=0,reserved=0 WHERE user_id=? AND id=?",
            (user_id, topic_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_topic_pinned(user_id: int, topic_id: int, pinned: bool) -> bool:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            """UPDATE tenant_topics
               SET pinned=?, reserved=0
               WHERE user_id=? AND id=? AND active=1""",
            (1 if pinned else 0, user_id, topic_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def pinned_topic_count(user_id: int) -> int:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM tenant_topics WHERE user_id=? AND active=1 AND pinned=1",
            (user_id,),
        )
        return int((await cur.fetchone())[0])


async def reserve_random_topic(user_id: int):
    """Берёт следующую закреплённую тему для публикации.

    Закреплённые темы идут по кругу: сначала ещё не использованные, затем та,
    которая использовалась давнее всего. При двух и более темах последняя
    опубликованная тема исключается, поэтому подряд она не повторится.
    """
    async with _DB_LOCK:
        async with aiosqlite.connect(_path()) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")

            cur = await conn.execute(
                """SELECT topic_id FROM tenant_publications
                   WHERE user_id=? AND topic_id IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            )
            last = await cur.fetchone()
            last_id = int(last["topic_id"]) if last and last["topic_id"] is not None else None

            cur = await conn.execute(
                """SELECT COUNT(*) FROM tenant_topics
                   WHERE user_id=? AND active=1 AND pinned=1 AND reserved=0""",
                (user_id,),
            )
            active_count = int((await cur.fetchone())[0])
            if active_count == 0:
                await conn.rollback()
                return None

            params: list[Any] = [user_id]
            exclude = ""
            if last_id is not None and active_count > 1:
                exclude = "AND id<>?"
                params.append(last_id)

            cur = await conn.execute(
                f"""
                SELECT * FROM tenant_topics
                WHERE user_id=? AND active=1 AND pinned=1 AND reserved=0 {exclude}
                ORDER BY CASE WHEN used_at IS NULL THEN 0 ELSE 1 END,
                         COALESCE(used_at, created_at) ASC,
                         id ASC
                LIMIT 1
                """,
                params,
            )
            row = await cur.fetchone()
            if row is None:
                await conn.rollback()
                return None
            await conn.execute(
                "UPDATE tenant_topics SET reserved=1 WHERE user_id=? AND id=?",
                (user_id, int(row["id"])),
            )
            await conn.commit()
            return row


async def release_topic(user_id: int, topic_id: int) -> None:
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            "UPDATE tenant_topics SET reserved=0 WHERE user_id=? AND id=?", (user_id, topic_id)
        )
        await conn.commit()


async def mark_topic_used(user_id: int, topic_id: int) -> None:
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            "UPDATE tenant_topics SET reserved=0,used_at=? WHERE user_id=? AND id=?",
            (_now(), user_id, topic_id),
        )
        await conn.commit()


async def list_schedule(user_id: int):
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tenant_schedule WHERE user_id=? AND enabled=1 ORDER BY publish_time", (user_id,)
        )
        return await cur.fetchall()


async def add_schedule_time(user_id: int, value: str) -> bool:
    value = value.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return False
    await _ensure_user_row(user_id)
    try:
        async with aiosqlite.connect(_path()) as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO tenant_schedule(user_id,publish_time,enabled) VALUES(?,?,1)",
                (user_id, value),
            )
            await conn.commit()
            return cur.rowcount > 0
    except aiosqlite.IntegrityError:
        return False


async def delete_schedule_time(user_id: int, schedule_id: int) -> bool:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "DELETE FROM tenant_schedule WHERE user_id=? AND id=?", (user_id, schedule_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def claim_due_users(publish_time: str, slot_key: str) -> list[int]:
    """Atomically claim a YYYY-MM-DD|HH:MM slot for active paid users."""
    now_iso = _now()
    claimed: list[int] = []
    async with _DB_LOCK:
        async with aiosqlite.connect(_path()) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                """
                SELECT DISTINCT s.user_id
                FROM tenant_schedule s
                JOIN subscriptions sub ON sub.user_id=s.user_id
                LEFT JOIN tenant_settings a
                  ON a.user_id=s.user_id AND a.key='auto_publish_enabled'
                LEFT JOIN tenant_settings l
                  ON l.user_id=s.user_id AND l.key='last_auto_slot'
                WHERE s.enabled=1
                  AND s.publish_time=?
                  AND sub.status='active'
                  AND sub.expires_at IS NOT NULL
                  AND sub.expires_at>?
                  AND COALESCE(a.value,'1')='1'
                  AND COALESCE(l.value,'')<>?
                """,
                (publish_time, now_iso, slot_key),
            )
            rows = await cur.fetchall()
            for row in rows:
                uid = int(row["user_id"])
                await conn.execute(
                    """
                    INSERT INTO tenant_settings(user_id,key,value) VALUES(?, 'last_auto_slot', ?)
                    ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value
                    """,
                    (uid, slot_key),
                )
                claimed.append(uid)
            await conn.commit()
    return claimed


async def create_publication(
    *,
    user_id: int,
    topic_id: int | None,
    topic_title: str,
    article_title: str,
    article_body: str,
    image_path: str | None,
    trigger_type: str,
) -> int:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            """
            INSERT INTO tenant_publications(
                user_id,topic_id,topic_title,article_title,article_body,image_path,
                trigger_type,status,created_at
            ) VALUES(?,?,?,?,?,?,?,'pending',?)
            """,
            (user_id, topic_id, topic_title, article_title, article_body, image_path, trigger_type, _now()),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def finish_publication(
    publication_id: int,
    *,
    status: str,
    channels_published: int = 0,
    error: str | None = None,
) -> None:
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            "UPDATE tenant_publications SET status=?,channels_published=?,error=? WHERE id=?",
            (status, channels_published, error, publication_id),
        )
        await conn.commit()


async def stats(user_id: int) -> dict[str, Any]:
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        total = int((await (await conn.execute(
            "SELECT COUNT(*) FROM tenant_publications WHERE user_id=?", (user_id,)
        )).fetchone())[0])
        published = int((await (await conn.execute(
            "SELECT COUNT(*) FROM tenant_publications WHERE user_id=? AND status='published'", (user_id,)
        )).fetchone())[0])
        topics = int((await (await conn.execute(
            "SELECT COUNT(*) FROM tenant_topics WHERE user_id=? AND active=1", (user_id,)
        )).fetchone())[0])
        channels = int((await (await conn.execute(
            "SELECT COUNT(*) FROM tenant_channels WHERE user_id=? AND active=1", (user_id,)
        )).fetchone())[0])
        cur = await conn.execute(
            "SELECT article_title FROM tenant_publications WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        return {
            "total": total,
            "published": published,
            "topics": topics,
            "channels": channels,
            "last_title": row["article_title"] if row else None,
        }


async def platform_stats() -> dict[str, int]:
    now = _now()
    async with aiosqlite.connect(_path()) as conn:
        users = int((await (await conn.execute("SELECT COUNT(*) FROM app_users")).fetchone())[0])
        active = int((await (await conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires_at>?", (now,)
        )).fetchone())[0])
        channels = int((await (await conn.execute(
            "SELECT COUNT(*) FROM tenant_channels WHERE active=1"
        )).fetchone())[0])
        payments = int((await (await conn.execute("SELECT COUNT(*) FROM payments")).fetchone())[0])
        stars = int((await (await conn.execute(
            "SELECT COALESCE(SUM(total_amount),0) FROM payments WHERE currency='XTR'"
        )).fetchone())[0])
        return {
            "users": users,
            "active_subscriptions": active,
            "channels": channels,
            "payments": payments,
            "stars": stars,
        }


async def list_users(limit: int = 30):
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT u.user_id,u.username,u.first_name,u.last_name,u.updated_at,
                   s.status,s.expires_at
            FROM app_users u
            LEFT JOIN subscriptions s ON s.user_id=u.user_id
            ORDER BY u.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return await cur.fetchall()
