from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import aiosqlite

DB_PATH: Path | None = None
_DB_LOCK = asyncio.Lock()

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _key(title: str) -> str:
    return " ".join(title.split()).casefold()

def _path() -> Path:
    if DB_PATH is None:
        raise RuntimeError("БД не инициализирована")
    return DB_PATH

async def init_db(path: Path) -> None:
    global DB_PATH
    DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        await conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            title_key TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            reserved INTEGER NOT NULL DEFAULT 0,
            used_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publish_time TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER,
            topic_title TEXT NOT NULL,
            article_title TEXT NOT NULL,
            article_body TEXT NOT NULL,
            image_path TEXT,
            trigger_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            telegram_status TEXT NOT NULL DEFAULT 'pending',
            telegram_message_id TEXT,
            telegram_error TEXT,
            FOREIGN KEY(topic_id) REFERENCES topics(id)
        );
        """)
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('auto_publish_enabled','1')"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('last_auto_slot','')"
        )
        await conn.commit()

async def seed_topics(titles: Iterable[str]) -> int:
    count = 0
    async with aiosqlite.connect(_path()) as conn:
        for raw in titles:
            title = " ".join(str(raw).split())
            if not title:
                continue
            cur = await conn.execute(
                "INSERT OR IGNORE INTO topics(title,title_key,created_at) VALUES(?,?,?)",
                (title, _key(title), _now()),
            )
            if cur.rowcount:
                count += 1
        await conn.commit()
    return count

async def add_topics(titles: Iterable[str]) -> int:
    return await seed_topics(titles)

async def list_topics(mode: str = "unused", limit: int = 50):
    conditions = ["active=1"]
    if mode == "unused":
        conditions.append("used_at IS NULL")
    elif mode == "used":
        conditions.append("used_at IS NOT NULL")
    sql = f"""
        SELECT id,title,active,reserved,used_at,created_at
        FROM topics
        WHERE {' AND '.join(conditions)}
        ORDER BY CASE WHEN used_at IS NULL THEN 0 ELSE 1 END,
                 COALESCE(used_at,created_at) DESC, id DESC
        LIMIT ?
    """
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, (limit,))
        return await cur.fetchall()

async def get_topic(topic_id: int):
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,))
        return await cur.fetchone()

async def update_topic(topic_id: int, new_title: str) -> bool:
    title = " ".join(new_title.split())
    if not title:
        return False
    try:
        async with aiosqlite.connect(_path()) as conn:
            cur = await conn.execute(
                "UPDATE topics SET title=?,title_key=? WHERE id=?",
                (title, _key(title), topic_id),
            )
            await conn.commit()
            return cur.rowcount > 0
    except aiosqlite.IntegrityError:
        return False

async def deactivate_topic(topic_id: int) -> bool:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "UPDATE topics SET active=0,reserved=0 WHERE id=?",
            (topic_id,),
        )
        await conn.commit()
        return cur.rowcount > 0


async def _ensure_priority_columns() -> None:
    async with aiosqlite.connect(
        _path()
    ) as conn:
        cur = await conn.execute(
            "PRAGMA table_info(topics)"
        )

        columns = {
            str(row[1])
            for row in await cur.fetchall()
        }

        if "priority" not in columns:
            await conn.execute(
                """
                ALTER TABLE topics
                ADD COLUMN priority
                INTEGER NOT NULL DEFAULT 0
                """
            )

        if "priority_at" not in columns:
            await conn.execute(
                """
                ALTER TABLE topics
                ADD COLUMN priority_at TEXT
                """
            )

        await conn.commit()


async def add_priority_topics(
    titles: Iterable[str],
) -> int:
    await _ensure_priority_columns()

    now = _now()
    count = 0
    seen = set()

    async with aiosqlite.connect(
        _path()
    ) as conn:

        for raw in titles:
            title = " ".join(
                str(raw).split()
            )

            if not title:
                continue

            key = _key(title)

            if key in seen:
                continue

            seen.add(key)

            await conn.execute(
                """
                INSERT OR IGNORE INTO topics(
                    title,
                    title_key,
                    active,
                    reserved,
                    used_at,
                    created_at,
                    priority,
                    priority_at
                )
                VALUES(?,?,1,0,NULL,?,1,?)
                """,
                (
                    title,
                    key,
                    now,
                    now,
                ),
            )

            await conn.execute(
                """
                UPDATE topics
                SET
                    title=?,
                    active=1,
                    priority=1,
                    priority_at=?
                WHERE title_key=?
                """,
                (
                    title,
                    now,
                    key,
                ),
            )

            count += 1

        await conn.commit()

    return count


async def list_priority_topics(
    limit: int = 50,
):
    await _ensure_priority_columns()

    async with aiosqlite.connect(
        _path()
    ) as conn:
        conn.row_factory = aiosqlite.Row

        cur = await conn.execute(
            """
            SELECT *
            FROM topics
            WHERE active=1
              AND priority=1
            ORDER BY
                priority_at ASC,
                id ASC
            LIMIT ?
            """,
            (int(limit),),
        )

        return await cur.fetchall()


async def clear_topic_priority(
    topic_id: int,
) -> bool:
    await _ensure_priority_columns()

    async with aiosqlite.connect(
        _path()
    ) as conn:
        cur = await conn.execute(
            """
            UPDATE topics
            SET
                priority=0,
                priority_at=NULL
            WHERE id=?
            """,
            (int(topic_id),),
        )

        await conn.commit()

        return cur.rowcount > 0



async def reserve_random_topic():
    """
    Выбирает тему для очередной плановой статьи.

    Сначала всегда выбираются приоритетные темы.
    Обычная очередь используется только тогда,
    когда приоритетных тем больше нет.
    """

    await _ensure_priority_columns()

    async with _DB_LOCK:
        async with aiosqlite.connect(
            _path()
        ) as conn:
            conn.row_factory = aiosqlite.Row

            await conn.execute(
                "BEGIN IMMEDIATE"
            )

            cur = await conn.execute(
                """
                SELECT topic_id
                FROM publications
                WHERE topic_id IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """
            )

            last_publication = (
                await cur.fetchone()
            )

            last_topic_id = (
                int(
                    last_publication[
                        "topic_id"
                    ]
                )
                if last_publication
                else None
            )

            # ---------------------------------
            # СНАЧАЛА ПРИОРИТЕТНАЯ ОЧЕРЕДЬ
            # ---------------------------------

            cur = await conn.execute(
                """
                SELECT COUNT(*)
                FROM topics
                WHERE active=1
                  AND reserved=0
                  AND priority=1
                """
            )

            priority_count = int(
                (await cur.fetchone())[0]
            )

            row = None

            if priority_count > 0:
                params = []
                exclude = ""

                # Если срочных тем несколько,
                # стараемся не повторять
                # последнюю статью подряд.
                if (
                    last_topic_id is not None
                    and priority_count > 1
                ):
                    exclude = "AND id <> ?"
                    params.append(
                        last_topic_id
                    )

                cur = await conn.execute(
                    f"""
                    SELECT *
                    FROM topics
                    WHERE active=1
                      AND reserved=0
                      AND priority=1
                      {exclude}
                    ORDER BY
                        priority_at ASC,
                        id ASC
                    LIMIT 1
                    """,
                    tuple(params),
                )

                row = await cur.fetchone()

            # ---------------------------------
            # ОБЫЧНАЯ ОЧЕРЕДЬ
            # ---------------------------------

            if row is None:
                cur = await conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM topics
                    WHERE active=1
                      AND reserved=0
                    """
                )

                active_count = int(
                    (await cur.fetchone())[0]
                )

                params = []
                exclude = ""

                if (
                    last_topic_id is not None
                    and active_count > 1
                ):
                    exclude = "AND id <> ?"
                    params.append(
                        last_topic_id
                    )

                cur = await conn.execute(
                    f"""
                    SELECT *
                    FROM topics
                    WHERE active=1
                      AND reserved=0
                      {exclude}
                    ORDER BY
                        CASE
                            WHEN used_at IS NULL
                            THEN 0
                            ELSE 1
                        END ASC,
                        used_at ASC,
                        RANDOM()
                    LIMIT 1
                    """,
                    tuple(params),
                )

                row = await cur.fetchone()

            if row is None:
                await conn.rollback()
                return None

            upd = await conn.execute(
                """
                UPDATE topics
                SET reserved=1
                WHERE id=?
                  AND reserved=0
                  AND active=1
                """,
                (row["id"],),
            )

            if upd.rowcount != 1:
                await conn.rollback()
                return None

            await conn.commit()

            return dict(row)

async def release_topic(topic_id: int) -> None:
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute(
            "UPDATE topics SET reserved=0 WHERE id=?",
            (topic_id,),
        )
        await conn.commit()



async def mark_topic_used(
    topic_id: int,
) -> None:
    await _ensure_priority_columns()

    async with aiosqlite.connect(
        _path()
    ) as conn:
        await conn.execute(
            """
            UPDATE topics
            SET
                reserved=0,
                used_at=?,
                priority=0,
                priority_at=NULL
            WHERE id=?
            """,
            (
                _now(),
                int(topic_id),
            ),
        )

        await conn.commit()

async def add_manual_used_topic(title: str) -> int:
    title = " ".join(title.split())
    key = _key(title)
    now = _now()
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            INSERT OR IGNORE INTO topics(title,title_key,active,reserved,used_at,created_at)
            VALUES(?,?,1,0,?,?)
        """, (title, key, now, now))
        await conn.execute("""
            UPDATE topics
            SET active=1,reserved=0,used_at=?
            WHERE title_key=?
        """, (now, key))
        cur = await conn.execute("SELECT id FROM topics WHERE title_key=?", (key,))
        row = await cur.fetchone()
        await conn.commit()
        return int(row["id"])

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute("""
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        await conn.commit()

async def auto_publish_enabled() -> bool:
    return (await get_setting("auto_publish_enabled", "1")) == "1"

async def set_auto_publish_enabled(enabled: bool) -> None:
    await set_setting("auto_publish_enabled", "1" if enabled else "0")

async def ensure_default_daily_schedule(
    default_times: tuple[str, ...] = (
        "09:00",
        "12:00",
        "15:00",
        "18:00",
        "21:00",
    ),
) -> None:
    """
    Инициализирует пять ежедневных автоматических публикаций.

    По умолчанию:
      09:00
      14:00
      19:00

    При обновлении:
    - старый единственный дефолт 19:00 заменяется на три новых слота;
    - старый единственный 10:00 тоже заменяется;
    - пользовательское расписание из нескольких слотов не перезаписывается.
    """
    if not default_times:
        raise ValueError(
            "DEFAULT_PUBLISH_TIMES не должен быть пустым"
        )

    normalized: list[str] = []

    for value in default_times:
        value = value.strip()

        try:
            datetime.strptime(
                value,
                "%H:%M",
            )
        except ValueError as exc:
            raise ValueError(
                f"Некорректное время в DEFAULT_PUBLISH_TIMES: {value}"
            ) from exc

        if value not in normalized:
            normalized.append(
                value
            )

    migration_key = "daily_schedule_v58_five_articles"

    if await get_setting(
        migration_key,
        "",
    ) == "1":
        rows = await list_schedule()

        if not rows:
            for value in normalized:
                await add_schedule_time(
                    value
                )
        return

    async with aiosqlite.connect(
        _path()
    ) as conn:
        conn.row_factory = aiosqlite.Row

        cur = await conn.execute("""
            SELECT id,publish_time,enabled
            FROM schedule
            WHERE enabled=1
            ORDER BY publish_time
        """)
        rows = await cur.fetchall()

        existing = [
            row["publish_time"]
            for row in rows
        ]

        should_replace_old_default = (
            not rows
            or (
                len(rows) == 1
                and existing[0] in {
                    "10:00",
                    "19:00",
                }
            )
            or existing == [
                "09:00",
                "14:00",
                "19:00",
            ]
        )

        if should_replace_old_default:
            await conn.execute(
                "DELETE FROM schedule"
            )

            for value in normalized:
                await conn.execute(
                    "INSERT OR IGNORE INTO schedule(publish_time,enabled) "
                    "VALUES(?,1)",
                    (value,),
                )

        await conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                migration_key,
                "1",
            ),
        )

        await conn.commit()



async def list_schedule():
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT id,publish_time,enabled FROM schedule
            WHERE enabled=1 ORDER BY publish_time
        """)
        return await cur.fetchall()

async def add_schedule_time(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO schedule(publish_time,enabled) VALUES(?,1)",
            (value,),
        )
        await conn.commit()
        return cur.rowcount > 0

async def delete_schedule_time(schedule_id: int) -> bool:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute("DELETE FROM schedule WHERE id=?", (schedule_id,))
        await conn.commit()
        return cur.rowcount > 0


async def _ensure_subtopic_history() -> None:
    async with aiosqlite.connect(
        _path()
    ) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            article_subtopic_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER,
                parent_topic TEXT NOT NULL,
                subtopic TEXT NOT NULL,
                subtopic_key TEXT NOT NULL UNIQUE,
                published_at TEXT NOT NULL
            )
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_article_subtopic_history_date
            ON article_subtopic_history(
                published_at DESC
            )
            """
        )

        await conn.commit()


async def list_used_subtopics(
    parent_topic: str,
    limit: int = 80,
) -> list[str]:
    await _ensure_subtopic_history()

    parent_key = _key(
        parent_topic
    )

    async with aiosqlite.connect(
        _path()
    ) as conn:
        cur = await conn.execute(
            """
            SELECT subtopic
            FROM article_subtopic_history
            WHERE lower(
                trim(parent_topic)
            )=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                parent_key,
                int(limit),
            ),
        )

        rows = await cur.fetchall()

    return [
        str(row[0])
        for row in rows
        if row and row[0]
    ]


async def record_used_subtopic(
    topic_id: int | None,
    parent_topic: str,
    subtopic: str,
) -> None:
    await _ensure_subtopic_history()

    value = " ".join(
        str(subtopic).split()
    )

    if not value:
        return

    async with aiosqlite.connect(
        _path()
    ) as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO
            article_subtopic_history(
                topic_id,
                parent_topic,
                subtopic,
                subtopic_key,
                published_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                int(topic_id)
                if topic_id is not None
                else None,
                " ".join(
                    str(parent_topic).split()
                ),
                value,
                _key(value),
                _now(),
            ),
        )

        await conn.commit()


async def create_publication(
    *,
    topic_id: int | None,
    topic_title: str,
    article_title: str,
    article_body: str,
    image_path: str | None,
    trigger_type: str,
) -> int:
    async with aiosqlite.connect(_path()) as conn:
        cur = await conn.execute("""
            INSERT INTO publications(
                topic_id,topic_title,article_title,article_body,
                image_path,trigger_type,created_at
            ) VALUES(?,?,?,?,?,?,?)
        """, (
            topic_id, topic_title, article_title, article_body,
            image_path, trigger_type, _now()
        ))
        await conn.commit()
        return int(cur.lastrowid)

async def set_telegram_result(
    publication_id: int,
    status: str,
    message_id: str | None = None,
    error: str | None = None,
) -> None:
    async with aiosqlite.connect(_path()) as conn:
        await conn.execute("""
            UPDATE publications
            SET telegram_status=?,telegram_message_id=?,telegram_error=?
            WHERE id=?
        """, (status, message_id, error, publication_id))
        await conn.commit()

async def stats() -> dict:
    async with aiosqlite.connect(_path()) as conn:
        conn.row_factory = aiosqlite.Row

        async def scalar(sql: str):
            cur = await conn.execute(sql)
            row = await cur.fetchone()
            return row[0] if row else 0

        total = await scalar("SELECT COUNT(*) FROM publications")
        active = await scalar("SELECT COUNT(*) FROM topics WHERE active=1")
        unused = await scalar("SELECT COUNT(*) FROM topics WHERE active=1 AND used_at IS NULL")
        used = await scalar("SELECT COUNT(*) FROM topics WHERE active=1 AND used_at IS NOT NULL")
        tg_ok = await scalar("SELECT COUNT(*) FROM publications WHERE telegram_status='published'")

        cur = await conn.execute("""
            SELECT article_title,created_at FROM publications
            ORDER BY id DESC LIMIT 1
        """)
        last = await cur.fetchone()

        return {
            "total": total,
            "active": active,
            "unused": unused,
            "used": used,
            "telegram_published": tg_ok,
            "last_title": last["article_title"] if last else None,
            "last_created_at": last["created_at"] if last else None,
        }
