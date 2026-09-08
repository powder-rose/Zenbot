from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


# =========================================================
# ТАРИФЫ
# =========================================================
# Эффективные цены взяты из фактического Billing Yandex Cloud.
# Можно менять через .env без правки кода.

GPT_INPUT_PER_1K_RUB = float(
    os.getenv("AI_PRICE_GPT_INPUT_PER_1K_RUB", "1.033")
)

GPT_CACHED_PER_1K_RUB = float(
    os.getenv("AI_PRICE_GPT_CACHED_PER_1K_RUB", "1.141")
)

GPT_OUTPUT_PER_1K_RUB = float(
    os.getenv("AI_PRICE_GPT_OUTPUT_PER_1K_RUB", "1.103")
)

IMAGE_PER_REQUEST_RUB = float(
    os.getenv("AI_PRICE_IMAGE_RUB", "1.784")
)

SEARCH_PER_REQUEST_RUB = float(
    os.getenv("AI_PRICE_SEARCH_RUB", "0.354")
)


# =========================================================
# КОНТЕКСТ ДЕЙСТВИЯ
# =========================================================

_usage_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "ai_usage_context",
    default=None,
)


@contextmanager
def usage_context(
    action: str,
    *,
    user_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    """
    Помечает AI-вызовы внутри блока конкретным действием.

    Примеры action:
        article_full
        article_short
        subtopic_select
        comment_reply
        popular_comment_topic
        image_generation
        search
    """

    token = _usage_context.set(
        {
            "action": str(action).strip() or "unknown",
            "user_id": user_id,
            "metadata": dict(metadata or {}),
        }
    )

    try:
        yield
    finally:
        _usage_context.reset(token)


def current_context() -> dict[str, Any]:
    value = _usage_context.get()

    if not value:
        return {
            "action": "unknown",
            "user_id": None,
            "metadata": {},
        }

    return {
        "action": value.get("action") or "unknown",
        "user_id": value.get("user_id"),
        "metadata": dict(value.get("metadata") or {}),
    }


# =========================================================
# DATABASE
# =========================================================

def _db_path() -> Path:
    return Path(
        os.getenv(
            "DB_PATH",
            "data/bot.db",
        )
    )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        str(path),
        timeout=30,
    )

    conn.execute(
        "PRAGMA busy_timeout = 30000"
    )

    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                created_at TEXT NOT NULL,

                user_id INTEGER,

                action TEXT NOT NULL,
                service TEXT NOT NULL,
                model TEXT,

                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,

                requests INTEGER NOT NULL DEFAULT 1,

                cost_rub REAL NOT NULL DEFAULT 0,

                pricing_json TEXT,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS
                idx_ai_usage_created_at
            ON ai_usage(created_at);

            CREATE INDEX IF NOT EXISTS
                idx_ai_usage_user
            ON ai_usage(user_id);

            CREATE INDEX IF NOT EXISTS
                idx_ai_usage_action
            ON ai_usage(action);

            CREATE INDEX IF NOT EXISTS
                idx_ai_usage_service
            ON ai_usage(service);
            """
        )


# =========================================================
# GPT
# =========================================================

def calculate_gpt_cost(
    *,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> float:
    """
    input_tokens считаем общим входом.

    Если API сообщает cached_tokens отдельно,
    они вычитаются из обычного входа и считаются
    по отдельному тарифу.
    """

    input_tokens = max(
        int(input_tokens or 0),
        0,
    )

    cached_tokens = max(
        int(cached_tokens or 0),
        0,
    )

    output_tokens = max(
        int(output_tokens or 0),
        0,
    )

    cached_tokens = min(
        cached_tokens,
        input_tokens,
    )

    uncached_input = (
        input_tokens
        - cached_tokens
    )

    cost = (
        uncached_input
        / 1000
        * GPT_INPUT_PER_1K_RUB
        +
        cached_tokens
        / 1000
        * GPT_CACHED_PER_1K_RUB
        +
        output_tokens
        / 1000
        * GPT_OUTPUT_PER_1K_RUB
    )

    return round(
        cost,
        6,
    )


def record_gpt(
    *,
    input_tokens: int,
    cached_tokens: int = 0,
    output_tokens: int,
    model: str = "yandexgpt",
    metadata: dict[str, Any] | None = None,
) -> float:

    context = current_context()

    cost = calculate_gpt_cost(
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
    )

    pricing = {
        "input_per_1k_rub":
            GPT_INPUT_PER_1K_RUB,

        "cached_per_1k_rub":
            GPT_CACHED_PER_1K_RUB,

        "output_per_1k_rub":
            GPT_OUTPUT_PER_1K_RUB,
    }

    combined_metadata = dict(
        context["metadata"]
    )

    combined_metadata.update(
        metadata or {}
    )

    _insert(
        user_id=context["user_id"],
        action=context["action"],
        service="yandexgpt",
        model=model,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        requests=1,
        cost_rub=cost,
        pricing=pricing,
        metadata=combined_metadata,
    )

    return cost


# =========================================================
# IMAGE
# =========================================================

def record_image(
    *,
    model: str = "yandex-art",
    metadata: dict[str, Any] | None = None,
) -> float:

    context = current_context()

    cost = round(
        IMAGE_PER_REQUEST_RUB,
        6,
    )

    combined_metadata = dict(
        context["metadata"]
    )

    combined_metadata.update(
        metadata or {}
    )

    _insert(
        user_id=context["user_id"],
        action=context["action"],
        service="yandex_art",
        model=model,
        requests=1,
        cost_rub=cost,
        pricing={
            "request_rub":
                IMAGE_PER_REQUEST_RUB,
        },
        metadata=combined_metadata,
    )

    return cost


# =========================================================
# SEARCH
# =========================================================

def record_search(
    *,
    metadata: dict[str, Any] | None = None,
) -> float:

    context = current_context()

    cost = round(
        SEARCH_PER_REQUEST_RUB,
        6,
    )

    combined_metadata = dict(
        context["metadata"]
    )

    combined_metadata.update(
        metadata or {}
    )

    _insert(
        user_id=context["user_id"],
        action=context["action"],
        service="yandex_search",
        model="search-api",
        requests=1,
        cost_rub=cost,
        pricing={
            "request_rub":
                SEARCH_PER_REQUEST_RUB,
        },
        metadata=combined_metadata,
    )

    return cost


# =========================================================
# СЕБЕСТОИМОСТЬ СТАТЬИ
# =========================================================

_ARTICLE_COST_ACTIONS = {
    "search_subtopic",
    "subtopic_select",
    "search_article",
    "search_article_fresh_retry",
    "article_full",
    "article_short",
    "image_generation",
    "popular_comment_topic",
}




# =========================================================
# INSERT
# =========================================================

def _insert(
    *,
    user_id: int | None,
    action: str,
    service: str,
    model: str | None = None,
    input_tokens: int = 0,
    cached_tokens: int = 0,
    output_tokens: int = 0,
    requests: int = 1,
    cost_rub: float = 0,
    pricing: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:

    init_db()

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO ai_usage (
                created_at,
                user_id,
                action,
                service,
                model,
                input_tokens,
                cached_tokens,
                output_tokens,
                requests,
                cost_rub,
                pricing_json,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                user_id,
                action,
                service,
                model,
                int(input_tokens or 0),
                int(cached_tokens or 0),
                int(output_tokens or 0),
                int(requests or 0),
                float(cost_rub or 0),
                json.dumps(
                    pricing or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    metadata or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )


# =========================================================
# REPORTS
# =========================================================

def totals() -> dict[str, Any]:
    init_db()

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS operations,
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(cached_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cost_rub), 0)
            FROM ai_usage
            """
        ).fetchone()

    return {
        "operations": int(row[0] or 0),
        "requests": int(row[1] or 0),
        "input_tokens": int(row[2] or 0),
        "cached_tokens": int(row[3] or 0),
        "output_tokens": int(row[4] or 0),
        "cost_rub": round(
            float(row[5] or 0),
            4,
        ),
    }


def totals_by_action(
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    init_db()

    where = ""
    params: tuple[Any, ...] = ()

    if user_id is not None:
        where = "WHERE user_id = ?"
        params = (int(user_id),)

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                action,
                service,
                COUNT(*) AS operations,
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(cached_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cost_rub), 0)
            FROM ai_usage
            {where}
            GROUP BY
                action,
                service
            ORDER BY
                SUM(cost_rub) DESC,
                action ASC
            """,
            params,
        ).fetchall()

    return [
        {
            "action": row[0],
            "service": row[1],
            "operations": int(row[2] or 0),
            "requests": int(row[3] or 0),
            "input_tokens": int(row[4] or 0),
            "cached_tokens": int(row[5] or 0),
            "output_tokens": int(row[6] or 0),
            "cost_rub": round(
                float(row[7] or 0),
                4,
            ),
        }
        for row in rows
    ]


def recent_operations(
    *,
    limit: int = 50,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    init_db()

    limit = max(
        1,
        min(int(limit), 500),
    )

    where = ""
    params: list[Any] = []

    if user_id is not None:
        where = "WHERE user_id = ?"
        params.append(int(user_id))

    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id,
                created_at,
                user_id,
                action,
                service,
                model,
                input_tokens,
                cached_tokens,
                output_tokens,
                requests,
                cost_rub,
                metadata_json
            FROM ai_usage
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    result = []

    for row in rows:
        try:
            metadata = json.loads(
                row[11] or "{}"
            )
        except Exception:
            metadata = {}

        result.append(
            {
                "id": int(row[0]),
                "created_at": row[1],
                "user_id": row[2],
                "action": row[3],
                "service": row[4],
                "model": row[5],
                "input_tokens": int(row[6] or 0),
                "cached_tokens": int(row[7] or 0),
                "output_tokens": int(row[8] or 0),
                "requests": int(row[9] or 0),
                "cost_rub": round(
                    float(row[10] or 0),
                    4,
                ),
                "metadata": metadata,
            }
        )

    return result



def period_report(
    period: str = "today",
    *,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """
    period:
        today  — текущий день
        month  — текущий месяц
        all    — всё время
    """

    init_db()

    period = str(period or "today").strip().lower()

    if period not in {
        "today",
        "month",
        "all",
    }:
        period = "today"

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    now_local = datetime.now(tz)

    start_utc: str | None = None

    if period == "today":
        start_local = now_local.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        start_utc = (
            start_local
            .astimezone(timezone.utc)
            .isoformat()
        )

    elif period == "month":
        start_local = now_local.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        start_utc = (
            start_local
            .astimezone(timezone.utc)
            .isoformat()
        )

    where = ""
    params: list[Any] = []

    if start_utc:
        where = "WHERE created_at >= ?"
        params.append(start_utc)

    with _connect() as conn:

        total = conn.execute(
            f"""
            SELECT
                COUNT(*),
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(cached_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cost_rub), 0)
            FROM ai_usage
            {where}
            """,
            tuple(params),
        ).fetchone()

        service_rows = conn.execute(
            f"""
            SELECT
                service,
                COUNT(*),
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(cost_rub), 0)
            FROM ai_usage
            {where}
            GROUP BY service
            ORDER BY SUM(cost_rub) DESC
            """,
            tuple(params),
        ).fetchall()

        action_rows = conn.execute(
            f"""
            SELECT
                action,
                service,
                COUNT(*),
                COALESCE(SUM(requests), 0),
                COALESCE(SUM(cost_rub), 0)
            FROM ai_usage
            {where}
            GROUP BY action, service
            ORDER BY SUM(cost_rub) DESC
            LIMIT 15
            """,
            tuple(params),
        ).fetchall()

        user_where = (
            f"{where} AND user_id IS NOT NULL"
            if where
            else "WHERE user_id IS NOT NULL"
        )

        user_rows = conn.execute(
            f"""
            SELECT
                user_id,
                COUNT(*),
                COALESCE(SUM(cost_rub), 0)
            FROM ai_usage
            {user_where}
            GROUP BY user_id
            ORDER BY SUM(cost_rub) DESC
            LIMIT 5
            """,
            tuple(params),
        ).fetchall()


        # -----------------------------------------
        # Средняя себестоимость опубликованной
        # статьи за выбранный период.
        # -----------------------------------------

        article_actions = sorted(
            _ARTICLE_COST_ACTIONS
        )

        placeholders = ",".join(
            "?"
            for _ in article_actions
        )

        if start_utc:
            article_cost_row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(cost_rub), 0)
                FROM ai_usage
                WHERE created_at >= ?
                  AND action IN ({placeholders})
                """,
                (
                    start_utc,
                    *article_actions,
                ),
            ).fetchone()
        else:
            article_cost_row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(cost_rub), 0)
                FROM ai_usage
                WHERE action IN ({placeholders})
                """,
                tuple(article_actions),
            ).fetchone()

        article_cost_rub = float(
            article_cost_row[0] or 0
        )


        # -----------------------------------------
        # Расходы и количество вызовов
        # по каждому этапу производства статьи.
        # -----------------------------------------

        if start_utc:
            article_stage_rows = conn.execute(
                f"""
                SELECT
                    action,
                    service,
                    COUNT(*),
                    COALESCE(SUM(requests), 0),
                    COALESCE(SUM(cost_rub), 0)
                FROM ai_usage
                WHERE created_at >= ?
                  AND action IN ({placeholders})
                GROUP BY action, service
                ORDER BY SUM(cost_rub) DESC
                """,
                (
                    start_utc,
                    *article_actions,
                ),
            ).fetchall()

        else:
            article_stage_rows = conn.execute(
                f"""
                SELECT
                    action,
                    service,
                    COUNT(*),
                    COALESCE(SUM(requests), 0),
                    COALESCE(SUM(cost_rub), 0)
                FROM ai_usage
                WHERE action IN ({placeholders})
                GROUP BY action, service
                ORDER BY SUM(cost_rub) DESC
                """,
                tuple(article_actions),
            ).fetchall()


        def table_exists(
            table_name: str,
        ) -> bool:
            row = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name=?
                LIMIT 1
                """,
                (
                    table_name,
                ),
            ).fetchone()

            return row is not None


        global_articles = 0

        if table_exists(
            "publications"
        ):
            if start_utc:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM publications
                    WHERE telegram_status='published'
                      AND created_at >= ?
                    """,
                    (
                        start_utc,
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM publications
                    WHERE telegram_status='published'
                    """
                ).fetchone()

            global_articles = int(
                row[0] or 0
            )


        tenant_articles = 0

        if table_exists(
            "tenant_publications"
        ):
            if start_utc:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM tenant_publications
                    WHERE status='published'
                      AND created_at >= ?
                    """,
                    (
                        start_utc,
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM tenant_publications
                    WHERE status='published'
                    """
                ).fetchone()

            tenant_articles = int(
                row[0] or 0
            )


        published_articles = (
            global_articles
            + tenant_articles
        )

        average_article_cost_rub = (
            article_cost_rub
            / published_articles
            if published_articles
            else 0.0
        )


        article_stages = []

        for row in article_stage_rows:

            operations = int(
                row[2] or 0
            )

            requests = int(
                row[3] or 0
            )

            cost = float(
                row[4] or 0
            )

            article_stages.append(
                {
                    "action": str(
                        row[0] or "unknown"
                    ),
                    "service": str(
                        row[1] or ""
                    ),
                    "operations": operations,
                    "requests": requests,
                    "cost_rub": round(
                        cost,
                        4,
                    ),
                    "cost_per_article": round(
                        (
                            cost
                            / published_articles
                        )
                        if published_articles
                        else 0.0,
                        4,
                    ),
                    "operations_per_article": round(
                        (
                            operations
                            / published_articles
                        )
                        if published_articles
                        else 0.0,
                        3,
                    ),
                    "requests_per_article": round(
                        (
                            requests
                            / published_articles
                        )
                        if published_articles
                        else 0.0,
                        3,
                    ),
                }
            )


        article_gpt_operations = sum(
            int(
                row.get(
                    "operations",
                    0,
                )
            )
            for row in article_stages
            if row.get(
                "service"
            ) == "yandexgpt"
        )

        article_gpt_calls_per_article = (
            article_gpt_operations
            / published_articles
            if published_articles
            else 0.0
        )

    return {
        "period": period,
        "start_utc": start_utc,

        "operations": int(total[0] or 0),
        "requests": int(total[1] or 0),

        "input_tokens":
            int(total[2] or 0),

        "cached_tokens":
            int(total[3] or 0),

        "output_tokens":
            int(total[4] or 0),

        "cost_rub": round(
            float(total[5] or 0),
            4,
        ),

        "published_articles":
            published_articles,

        "article_cost_rub": round(
            article_cost_rub,
            4,
        ),

        "average_article_cost_rub": round(
            average_article_cost_rub,
            4,
        ),

        "article_gpt_calls_per_article": round(
            article_gpt_calls_per_article,
            3,
        ),

        "article_stages":
            article_stages,

        "services": [
            {
                "service": row[0],
                "operations": int(row[1] or 0),
                "requests": int(row[2] or 0),
                "cost_rub": round(
                    float(row[3] or 0),
                    4,
                ),
            }
            for row in service_rows
        ],

        "actions": [
            {
                "action": row[0],
                "service": row[1],
                "operations": int(row[2] or 0),
                "requests": int(row[3] or 0),
                "cost_rub": round(
                    float(row[4] or 0),
                    4,
                ),
            }
            for row in action_rows
        ],

        "users": [
            {
                "user_id": int(row[0]),
                "operations": int(row[1] or 0),
                "cost_rub": round(
                    float(row[2] or 0),
                    4,
                ),
            }
            for row in user_rows
        ],
    }
