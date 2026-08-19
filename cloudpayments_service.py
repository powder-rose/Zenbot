from __future__ import annotations

import datetime as dtlib

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs

import aiosqlite
import httpx
from aiohttp import web
from aiogram import Bot


log = logging.getLogger("cloudpayments")


ORDERS_URL = "https://api.cloudpayments.ru/orders/create"


def _db_path() -> Path:
    return Path(
        os.getenv(
            "DB_PATH",
            "/app/data/bot.db",
        )
    )


def _public_id() -> str:
    return os.getenv(
        "CLOUDPAYMENTS_PUBLIC_ID",
        "",
    ).strip()


def _api_secret() -> str:
    return os.getenv(
        "CLOUDPAYMENTS_API_SECRET",
        "",
    ).strip()


def _price_rub() -> int:
    return max(
        1,
        int(
            os.getenv(
                "SUBSCRIPTION_PRICE_RUB",
                "1200",
            )
        ),
    )


def _webhook_port() -> int:
    return max(
        1,
        int(
            os.getenv(
                "CLOUDPAYMENTS_WEBHOOK_PORT",
                "8080",
            )
        ),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        result = datetime.fromisoformat(value)
    except Exception:
        return None

    if result.tzinfo is None:
        result = result.replace(
            tzinfo=timezone.utc
        )

    return result.astimezone(
        timezone.utc
    )


async def ensure_schema() -> None:
    path = _db_path()
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cloudpayments_orders (
                invoice_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount_kopecks INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',

                status TEXT NOT NULL DEFAULT 'pending',

                cloudpayments_order_id TEXT,
                payment_url TEXT,

                transaction_id TEXT UNIQUE,

                created_at TEXT NOT NULL,
                paid_at TEXT,
                expires_at TEXT,

                raw_payload TEXT
            );

            CREATE INDEX IF NOT EXISTS
            idx_cloudpayments_orders_user
            ON cloudpayments_orders(
                user_id,
                created_at
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                discount_percent INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_promo_codes (
                user_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cloudpayments_order_promos (
                invoice_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                discount_percent INTEGER NOT NULL,
                base_amount_kopecks INTEGER NOT NULL,
                discount_kopecks INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                invoice_id TEXT NOT NULL UNIQUE,
                transaction_id TEXT NOT NULL UNIQUE,
                discount_percent INTEGER NOT NULL,
                discount_kopecks INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                UNIQUE(code, user_id)
            );

            CREATE INDEX IF NOT EXISTS
            idx_promo_redemptions_code_user
            ON promo_redemptions(
                code,
                user_id
            );
            """
        )

        await db.commit()



def _normalize_promo_code(
    code: str,
) -> str:
    code = re.sub(
        r"[^A-Za-zА-Яа-я0-9_-]+",
        "",
        str(code or "").strip(),
    ).upper()

    return code[:40]


def _promo_quote(
    discount_percent: int,
) -> dict[str, int]:
    base = _price_rub() * 100

    discount = (
        base * int(discount_percent)
    ) // 100

    final = max(
        0,
        base - discount,
    )

    return {
        "base_kopecks": base,
        "discount_kopecks": discount,
        "final_kopecks": final,
    }


async def create_promo_code(
    code: str,
    discount_percent: int,
) -> dict:
    await ensure_schema()

    normalized = _normalize_promo_code(
        code
    )

    if len(normalized) < 2:
        raise ValueError(
            "Промокод слишком короткий"
        )

    discount_percent = int(
        discount_percent
    )

    if not 1 <= discount_percent <= 100:
        raise ValueError(
            "Скидка должна быть от 1 до 100%"
        )

    now = _now().isoformat(
        timespec="seconds"
    )

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        await db.execute(
            """
            INSERT INTO promo_codes(
                code,
                discount_percent,
                active,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?)
            ON CONFLICT(code)
            DO UPDATE SET
                discount_percent=
                    excluded.discount_percent,
                active=1,
                updated_at=
                    excluded.updated_at
            """,
            (
                normalized,
                discount_percent,
                1,
                now,
                now,
            ),
        )

        await db.commit()

    quote = _promo_quote(
        discount_percent
    )

    return {
        "code": normalized,
        "discount_percent":
            discount_percent,
        **quote,
    }


async def disable_promo_code(
    code: str,
) -> bool:
    await ensure_schema()

    normalized = _normalize_promo_code(
        code
    )

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        cur = await db.execute(
            """
            UPDATE promo_codes
            SET
                active=0,
                updated_at=?
            WHERE code=?
            """,
            (
                _now().isoformat(
                    timespec="seconds"
                ),
                normalized,
            ),
        )

        await db.execute(
            """
            DELETE FROM user_promo_codes
            WHERE code=?
            """,
            (normalized,),
        )

        await db.commit()

        return bool(cur.rowcount)


async def list_promo_codes() -> list[dict]:
    await ensure_schema()

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            """
            SELECT
                p.code,
                p.discount_percent,
                p.active,
                p.created_at,
                (
                    SELECT COUNT(*)
                    FROM promo_redemptions r
                    WHERE r.code=p.code
                ) AS uses
            FROM promo_codes p
            ORDER BY
                p.active DESC,
                p.created_at DESC
            """
        )

        rows = await cur.fetchall()

    return [
        dict(row)
        for row in rows
    ]


async def apply_user_promo(
    user_id: int,
    code: str,
) -> dict:
    await ensure_schema()

    user_id = int(user_id)

    normalized = _normalize_promo_code(
        code
    )

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            """
            SELECT
                code,
                discount_percent,
                active
            FROM promo_codes
            WHERE code=?
            """,
            (normalized,),
        )

        promo = await cur.fetchone()

        if not promo or not int(
            promo["active"]
        ):
            return {
                "ok": False,
                "reason": "not_found",
            }

        cur = await db.execute(
            """
            SELECT 1
            FROM promo_redemptions
            WHERE code=?
              AND user_id=?
            LIMIT 1
            """,
            (
                normalized,
                user_id,
            ),
        )

        if await cur.fetchone():
            return {
                "ok": False,
                "reason": "already_used",
            }

        discount_percent = int(
            promo["discount_percent"]
        )

        quote = _promo_quote(
            discount_percent
        )

        # =================================================
        # 100% СКИДКА
        #
        # CloudPayments не нужен:
        # подписка активируется непосредственно,
        # а промокод сразу фиксируется использованным.
        # =================================================

        if discount_percent == 100:
            now_dt = _now()

            now_text = now_dt.isoformat(
                timespec="seconds"
            )

            cur = await db.execute(
                """
                SELECT
                    status,
                    starts_at,
                    expires_at
                FROM subscriptions
                WHERE user_id=?
                """,
                (user_id,),
            )

            subscription = (
                await cur.fetchone()
            )

            base_dt = now_dt
            starts_at = now_text

            if subscription:
                old_status = str(
                    subscription["status"]
                    or ""
                ).strip().lower()

                old_expiry_raw = str(
                    subscription["expires_at"]
                    or ""
                ).strip()

                if (
                    old_status == "active"
                    and old_expiry_raw
                ):
                    try:
                        old_expiry = (
                            dtlib.datetime.fromisoformat(
                                old_expiry_raw
                            )
                        )

                        if old_expiry.tzinfo is None:
                            old_expiry = (
                                old_expiry.replace(
                                    tzinfo=dtlib.timezone.utc
                                )
                            )

                        old_expiry = (
                            old_expiry.astimezone(
                                dtlib.timezone.utc
                            )
                        )

                        if old_expiry > now_dt:
                            base_dt = old_expiry

                            starts_at = str(
                                subscription[
                                    "starts_at"
                                ]
                                or now_text
                            )

                    except Exception:
                        pass

            expiry_dt = (
                base_dt
                + dtlib.timedelta(days=30)
            )

            expiry_text = (
                expiry_dt.isoformat(
                    timespec="seconds"
                )
            )

            invoice_id = (
                f"promo-free-"
                f"{user_id}-"
                f"{uuid.uuid4().hex}"
            )

            transaction_id = (
                f"promo-free:"
                f"{normalized}:"
                f"{user_id}:"
                f"{uuid.uuid4().hex}"
            )

            # На случай, если пользователь
            # ещё не успел попасть в app_users.
            await db.execute(
                """
                INSERT OR IGNORE INTO app_users(
                    user_id,
                    created_at,
                    updated_at
                )
                VALUES(?,?,?)
                """,
                (
                    user_id,
                    now_text,
                    now_text,
                ),
            )

            await db.execute(
                """
                INSERT INTO promo_redemptions(
                    code,
                    user_id,
                    invoice_id,
                    transaction_id,
                    discount_percent,
                    discount_kopecks,
                    redeemed_at
                )
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    normalized,
                    user_id,
                    invoice_id,
                    transaction_id,
                    100,
                    int(
                        quote[
                            "discount_kopecks"
                        ]
                    ),
                    now_text,
                ),
            )

            await db.execute(
                """
                INSERT INTO subscriptions(
                    user_id,
                    status,
                    starts_at,
                    expires_at,
                    source,
                    stars_amount,
                    telegram_payment_charge_id,
                    is_recurring,
                    updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    status='active',
                    starts_at=excluded.starts_at,
                    expires_at=excluded.expires_at,
                    source=excluded.source,
                    stars_amount=0,
                    telegram_payment_charge_id=NULL,
                    is_recurring=0,
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    "active",
                    starts_at,
                    expiry_text,
                    f"promo:{normalized}",
                    0,
                    None,
                    0,
                    now_text,
                ),
            )

            await db.execute(
                """
                DELETE FROM user_promo_codes
                WHERE user_id=?
                """,
                (user_id,),
            )

            await db.commit()

            log.info(
                "Free promo redeemed: "
                "user=%s code=%s expiry=%s",
                user_id,
                normalized,
                expiry_text,
            )

            return {
                "ok": True,
                "code": normalized,
                "discount_percent": 100,
                "base_kopecks":
                    quote[
                        "base_kopecks"
                    ],
                "discount_kopecks":
                    quote[
                        "discount_kopecks"
                    ],
                "final_kopecks": 0,
                "free": True,
                "activated": True,
                "expires_at": expiry_text,
            }

        # =================================================
        # ОБЫЧНЫЙ ПРОМОКОД 1–99%
        # =================================================

        await db.execute(
            """
            INSERT INTO user_promo_codes(
                user_id,
                code,
                applied_at
            )
            VALUES(?,?,?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                code=excluded.code,
                applied_at=
                    excluded.applied_at
            """,
            (
                user_id,
                normalized,
                _now().isoformat(
                    timespec="seconds"
                ),
            ),
        )

        await db.commit()

    return {
        "ok": True,
        "code": normalized,
        "discount_percent":
            discount_percent,
        "free": False,
        "activated": False,
        **quote,
    }



async def clear_user_promo(
    user_id: int,
) -> None:
    await ensure_schema()

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        await db.execute(
            """
            DELETE FROM user_promo_codes
            WHERE user_id=?
            """,
            (int(user_id),),
        )

        await db.commit()


async def get_user_promo(
    user_id: int,
) -> dict | None:
    await ensure_schema()

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            """
            SELECT
                p.code,
                p.discount_percent,
                p.active
            FROM user_promo_codes u
            JOIN promo_codes p
              ON p.code=u.code
            WHERE u.user_id=?
            """,
            (int(user_id),),
        )

        promo = await cur.fetchone()

        if not promo:
            return None

        cur = await db.execute(
            """
            SELECT 1
            FROM promo_redemptions
            WHERE code=?
              AND user_id=?
            LIMIT 1
            """,
            (
                promo["code"],
                int(user_id),
            ),
        )

        used = await cur.fetchone()

        if (
            not int(promo["active"])
            or used
        ):
            await db.execute(
                """
                DELETE FROM user_promo_codes
                WHERE user_id=?
                """,
                (int(user_id),),
            )

            await db.commit()

            return None

    quote = _promo_quote(
        int(promo["discount_percent"])
    )

    return {
        "code": promo["code"],
        "discount_percent":
            int(
                promo[
                    "discount_percent"
                ]
            ),
        **quote,
    }


async def create_cloudpayments_order(
    user_id: int,
) -> dict:
    public_id = _public_id()
    secret = _api_secret()

    if not public_id or not secret:
        raise RuntimeError(
            "CloudPayments ещё не настроен"
        )

    await ensure_schema()

    user_id = int(user_id)

    promo = await get_user_promo(
        user_id
    )

    base_amount_kopecks = (
        _price_rub() * 100
    )

    if promo:
        amount_kopecks = int(
            promo["final_kopecks"]
        )
    else:
        amount_kopecks = (
            base_amount_kopecks
        )

    # Если пользователь уже создал
    # неоплаченный счёт с этим же
    # промокодом, возвращаем его же,
    # а не создаём несколько ссылок.
    if promo:
        async with aiosqlite.connect(
            _db_path()
        ) as db:
            db.row_factory = (
                aiosqlite.Row
            )

            cur = await db.execute(
                """
                SELECT
                    o.invoice_id,
                    o.payment_url,
                    o.amount_kopecks
                FROM cloudpayments_orders o
                JOIN cloudpayments_order_promos p
                  ON p.invoice_id=o.invoice_id
                WHERE o.user_id=?
                  AND o.status='created'
                  AND p.code=?
                ORDER BY o.created_at DESC
                LIMIT 1
                """,
                (
                    user_id,
                    promo["code"],
                ),
            )

            existing = (
                await cur.fetchone()
            )

        if (
            existing
            and existing["payment_url"]
        ):
            return {
                "invoice_id":
                    existing[
                        "invoice_id"
                    ],
                "url":
                    existing[
                        "payment_url"
                    ],
                "amount_kopecks":
                    int(
                        existing[
                            "amount_kopecks"
                        ]
                    ),
                "base_amount_kopecks":
                    base_amount_kopecks,
                "promo_code":
                    promo["code"],
                "discount_percent":
                    promo[
                        "discount_percent"
                    ],
            }

    invoice_id = (
        f"zen-{user_id}-"
        f"{uuid.uuid4().hex}"
    )

    created_at = _now().isoformat(
        timespec="seconds"
    )

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        await db.execute(
            """
            INSERT INTO cloudpayments_orders(
                invoice_id,
                user_id,
                amount_kopecks,
                currency,
                status,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                invoice_id,
                user_id,
                amount_kopecks,
                "RUB",
                "pending",
                created_at,
            ),
        )

        if promo:
            await db.execute(
                """
                INSERT INTO
                    cloudpayments_order_promos(
                        invoice_id,
                        code,
                        discount_percent,
                        base_amount_kopecks,
                        discount_kopecks
                    )
                VALUES(?,?,?,?,?)
                """,
                (
                    invoice_id,
                    promo["code"],
                    int(
                        promo[
                            "discount_percent"
                        ]
                    ),
                    base_amount_kopecks,
                    int(
                        promo[
                            "discount_kopecks"
                        ]
                    ),
                ),
            )

        await db.commit()

    amount_rub = (
        Decimal(amount_kopecks)
        / Decimal(100)
    ).quantize(
        Decimal("0.00")
    )

    description = (
        "Подписка Дзен-бот — 30 дней"
    )

    if promo:
        description += (
            f" — промокод "
            f"{promo['code']}"
        )

    payload = {
        "Amount": float(amount_rub),
        "Currency": "RUB",
        "Description": description,
        "RequireConfirmation": False,
        "SendEmail": False,
        "InvoiceId": invoice_id,
        "AccountId": str(user_id),
        "CultureName": "ru-RU",
    }

    try:
        async with httpx.AsyncClient(
            timeout=30.0
        ) as client:
            response = await client.post(
                ORDERS_URL,
                json=payload,
                auth=(
                    public_id,
                    secret,
                ),
            )

            response.raise_for_status()

            data = response.json()

    except Exception:
        async with aiosqlite.connect(
            _db_path()
        ) as db:
            await db.execute(
                """
                UPDATE cloudpayments_orders
                SET status='create_failed'
                WHERE invoice_id=?
                """,
                (invoice_id,),
            )

            await db.commit()

        raise

    if not data.get("Success"):
        async with aiosqlite.connect(
            _db_path()
        ) as db:
            await db.execute(
                """
                UPDATE cloudpayments_orders
                SET status='create_failed'
                WHERE invoice_id=?
                """,
                (invoice_id,),
            )

            await db.commit()

        raise RuntimeError(
            str(
                data.get("Message")
                or
                "CloudPayments отклонил "
                "создание счёта"
            )
        )

    model = data.get("Model") or {}

    url = str(
        model.get("Url") or ""
    ).strip()

    cp_order_id = str(
        model.get("Id") or ""
    ).strip()

    if not url:
        raise RuntimeError(
            "CloudPayments не вернул "
            "ссылку оплаты"
        )

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        await db.execute(
            """
            UPDATE cloudpayments_orders
            SET
                status='created',
                cloudpayments_order_id=?,
                payment_url=?
            WHERE invoice_id=?
            """,
            (
                cp_order_id,
                url,
                invoice_id,
            ),
        )

        await db.commit()

    log.info(
        "CloudPayments order created: "
        "user=%s invoice=%s "
        "amount=%s promo=%s",
        user_id,
        invoice_id,
        amount_kopecks,
        (
            promo["code"]
            if promo
            else "-"
        ),
    )

    return {
        "invoice_id": invoice_id,
        "url": url,
        "amount_kopecks":
            amount_kopecks,
        "base_amount_kopecks":
            base_amount_kopecks,
        "promo_code":
            (
                promo["code"]
                if promo
                else None
            ),
        "discount_percent":
            (
                promo[
                    "discount_percent"
                ]
                if promo
                else 0
            ),
    }


def _amount_kopecks(
    value: str,
) -> int:
    try:
        amount = Decimal(
            str(value)
        ).quantize(
            Decimal("0.01")
        )
    except (
        InvalidOperation,
        ValueError,
    ):
        raise ValueError(
            "invalid amount"
        )

    return int(
        amount * 100
    )


async def _complete_payment(
    *,
    invoice_id: str,
    account_id: str,
    transaction_id: str,
    amount: str,
    currency: str,
    raw_payload: str,
) -> tuple[bool, int, datetime]:
    await ensure_schema()

    amount_kopecks = _amount_kopecks(
        amount
    )

    async with aiosqlite.connect(
        _db_path()
    ) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "BEGIN IMMEDIATE"
        )

        cur = await db.execute(
            """
            SELECT *
            FROM cloudpayments_orders
            WHERE invoice_id=?
            """,
            (invoice_id,),
        )

        order = await cur.fetchone()

        if not order:
            await db.rollback()
            raise ValueError(
                "unknown invoice"
            )

        if (
            str(account_id)
            != str(order["user_id"])
        ):
            await db.rollback()
            raise ValueError(
                "account mismatch"
            )

        if (
            str(currency).upper()
            != str(order["currency"]).upper()
        ):
            await db.rollback()
            raise ValueError(
                "currency mismatch"
            )

        if (
            amount_kopecks
            != int(order["amount_kopecks"])
        ):
            await db.rollback()
            raise ValueError(
                "amount mismatch"
            )

        order_status = str(
            order["status"] or ""
        ).strip().lower()

        if order_status not in {
            "pending",
            "created",
            "paid",
        }:
            await db.rollback()
            raise ValueError(
                "order is not payable"
            )

        if order["status"] == "paid":
            expiry = _dt(
                order["expires_at"]
            )

            if expiry is None:
                expiry = _now()

            await db.commit()

            return (
                False,
                int(order["user_id"]),
                expiry,
            )

        cur = await db.execute(
            """
            SELECT invoice_id
            FROM cloudpayments_orders
            WHERE transaction_id=?
            LIMIT 1
            """,
            (str(transaction_id),),
        )

        duplicate = await cur.fetchone()

        if duplicate:
            await db.rollback()
            raise ValueError(
                "duplicate transaction"
            )

        user_id = int(
            order["user_id"]
        )

        now = _now()
        base = now

        cur = await db.execute(
            """
            SELECT expires_at
            FROM subscriptions
            WHERE user_id=?
            """,
            (user_id,),
        )

        subscription = await cur.fetchone()

        if subscription:
            current_expiry = _dt(
                subscription["expires_at"]
            )

            if (
                current_expiry is not None
                and current_expiry > base
            ):
                base = current_expiry

        expiry = (
            base
            + timedelta(days=30)
        )

        now_text = now.isoformat(
            timespec="seconds"
        )

        expiry_text = expiry.isoformat(
            timespec="seconds"
        )

        await db.execute(
            """
            INSERT INTO subscriptions(
                user_id,
                status,
                starts_at,
                expires_at,
                source,
                stars_amount,
                telegram_payment_charge_id,
                is_recurring,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                status='active',
                expires_at=excluded.expires_at,
                source='cloudpayments',
                stars_amount=0,
                telegram_payment_charge_id=NULL,
                is_recurring=0,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                "active",
                now_text,
                expiry_text,
                "cloudpayments",
                0,
                None,
                0,
                now_text,
            ),
        )

        await db.execute(
            """
            UPDATE cloudpayments_orders
            SET
                status='paid',
                transaction_id=?,
                paid_at=?,
                expires_at=?,
                raw_payload=?
            WHERE invoice_id=?
            """,
            (
                str(transaction_id),
                now_text,
                expiry_text,
                raw_payload,
                invoice_id,
            ),
        )

        # Промокод считается использованным
        # ТОЛЬКО после успешного Pay webhook.
        cur = await db.execute(
            """
            SELECT
                code,
                discount_percent,
                discount_kopecks
            FROM cloudpayments_order_promos
            WHERE invoice_id=?
            """,
            (invoice_id,),
        )

        order_promo = await cur.fetchone()

        if order_promo:
            await db.execute(
                """
                INSERT OR IGNORE INTO
                    promo_redemptions(
                        code,
                        user_id,
                        invoice_id,
                        transaction_id,
                        discount_percent,
                        discount_kopecks,
                        redeemed_at
                    )
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    order_promo["code"],
                    user_id,
                    invoice_id,
                    str(transaction_id),
                    int(
                        order_promo[
                            "discount_percent"
                        ]
                    ),
                    int(
                        order_promo[
                            "discount_kopecks"
                        ]
                    ),
                    now_text,
                ),
            )

            await db.execute(
                """
                DELETE FROM user_promo_codes
                WHERE user_id=?
                  AND code=?
                """,
                (
                    user_id,
                    order_promo["code"],
                ),
            )

        await db.commit()

    return (
        True,
        user_id,
        expiry,
    )


class CloudPaymentsWebhookServer:
    def __init__(
        self,
        *,
        bot: Bot,
    ) -> None:
        self.bot = bot

        self.runner: (
            web.AppRunner | None
        ) = None

    async def start(self) -> None:
        await ensure_schema()

        app = web.Application()

        app.router.add_get(
            "/cloudpayments/health",
            self.health,
        )

        app.router.add_post(
            "/cloudpayments/pay",
            self.pay,
        )

        self.runner = web.AppRunner(
            app
        )

        await self.runner.setup()

        site = web.TCPSite(
            self.runner,
            "0.0.0.0",
            _webhook_port(),
        )

        await site.start()

        log.info(
            "CloudPayments webhook запущен: "
            "port=%s configured=%s",
            _webhook_port(),
            bool(
                _public_id()
                and _api_secret()
            ),
        )

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def health(
        self,
        request: web.Request,
    ) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "configured": bool(
                    _public_id()
                    and _api_secret()
                ),
            }
        )

    async def pay(
        self,
        request: web.Request,
    ) -> web.Response:
        secret = _api_secret()

        if not secret:
            return web.json_response(
                {"code": 13},
                status=503,
            )

        raw = await request.read()

        received_hmac = (
            request.headers.get(
                "Content-HMAC",
                "",
            ).strip()
        )

        expected_hmac = (
            base64.b64encode(
                hmac.new(
                    secret.encode(
                        "utf-8"
                    ),
                    raw,
                    hashlib.sha256,
                ).digest()
            ).decode("ascii")
        )

        if (
            not received_hmac
            or not hmac.compare_digest(
                received_hmac,
                expected_hmac,
            )
        ):
            log.warning(
                "CloudPayments webhook: "
                "неверная HMAC подпись"
            )

            return web.json_response(
                {"code": 13},
                status=403,
            )

        try:
            raw_text = raw.decode(
                "utf-8"
            )

            if (
                request.content_type
                == "application/json"
            ):
                payload = json.loads(
                    raw_text
                )
            else:
                parsed = parse_qs(
                    raw_text,
                    keep_blank_values=True,
                )

                payload = {
                    key: (
                        values[0]
                        if values
                        else ""
                    )
                    for key, values
                    in parsed.items()
                }

            transaction_id = str(
                payload.get(
                    "TransactionId",
                    "",
                )
            ).strip()

            invoice_id = str(
                payload.get(
                    "InvoiceId",
                    "",
                )
            ).strip()

            account_id = str(
                payload.get(
                    "AccountId",
                    "",
                )
            ).strip()

            amount = str(
                payload.get(
                    "Amount",
                    "",
                )
            ).strip()

            currency = str(
                payload.get(
                    "Currency",
                    "",
                )
            ).strip()

            if not all(
                (
                    transaction_id,
                    invoice_id,
                    account_id,
                    amount,
                    currency,
                )
            ):
                raise ValueError(
                    "missing required fields"
                )

            (
                activated,
                user_id,
                expiry,
            ) = await _complete_payment(
                invoice_id=invoice_id,
                account_id=account_id,
                transaction_id=transaction_id,
                amount=amount,
                currency=currency,
                raw_payload=raw_text,
            )

            if activated:
                log.info(
                    "CloudPayments paid: "
                    "user=%s invoice=%s "
                    "transaction=%s expiry=%s",
                    user_id,
                    invoice_id,
                    transaction_id,
                    expiry.isoformat(),
                )

                try:
                    await self.bot.send_message(
                        user_id,
                        "✅ <b>Оплата получена.</b>\n\n"
                        "Подписка активирована на "
                        "<b>30 дней</b>.\n"
                        f"Действует до: "
                        f"<code>{expiry.isoformat(timespec='seconds')}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    log.exception(
                        "Не удалось отправить "
                        "уведомление об оплате "
                        "user=%s",
                        user_id,
                    )

            return web.json_response(
                {"code": 0}
            )

        except Exception as exc:
            log.exception(
                "CloudPayments Pay webhook "
                "rejected: %s",
                exc,
            )

            return web.json_response(
                {"code": 13},
                status=400,
            )
