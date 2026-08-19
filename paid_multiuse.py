from __future__ import annotations

import asyncio

import html
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from tenant_help import (
    help_back_keyboard,
    help_home_text,
    help_keyboard,
    help_section_text,
)

import tenant_db
from cloudpayments_service import (
    apply_user_promo,
    clear_user_promo,
    create_cloudpayments_order,
    create_promo_code,
    disable_promo_code,
    list_promo_codes,
)
from tenant_dzen_qr_auth import authorize_tenant_dzen_by_qr
from article_service import DEFAULT_IMAGE_PROMPT_TEMPLATE
from config import Config
from tenant_service import TenantArticleService
from topics_seed import DEFAULT_TOPICS
from yandex_gpt import ARTICLE_SYSTEM_PROMPT

log = logging.getLogger(__name__)
router = Router(name="paid-multiuse")

_bot: Bot | None = None
_cfg: Config | None = None
_service: TenantArticleService | None = None
_dzen_auth_tasks: set[asyncio.Task[Any]] = set()


class PaidStates(StatesGroup):
    waiting_admin_promo_code = State()
    waiting_admin_promo_discount = State()
    waiting_promo_code = State()
    waiting_topics = State()
    waiting_edit_topic = State()
    waiting_schedule_time = State()
    waiting_urgent_topic = State()
    waiting_priority_topics = State()
    waiting_prompt_parts = State()
    waiting_dzen_comments_url = State()
    waiting_dzen_login = State()


PROMPTS = {
    "article": {
        "title": "✍️ Промпт текста статьи",
        "key": "prompt_article_system",
        "default": ARTICLE_SYSTEM_PROMPT,
        "filename": "article_prompt.txt",
    },
    "image": {
        "title": "🖼 Промпт изображения",
        "key": "prompt_image_template",
        "default": DEFAULT_IMAGE_PROMPT_TEMPLATE,
        "filename": "image_prompt.txt",
    },
}


def configure_paid_multiuse(
    *,
    bot: Bot,
    cfg: Config,
    service: TenantArticleService,
) -> None:
    global _bot, _cfg, _service
    _bot = bot
    _cfg = cfg
    _service = service


def _deps() -> tuple[Bot, Config, TenantArticleService]:
    if _bot is None or _cfg is None or _service is None:
        raise RuntimeError("Paid Multi-Use ещё не инициализирован")
    return _bot, _cfg, _service


def is_superadmin(user_id: int | None) -> bool:
    if not user_id or _cfg is None:
        return False
    return user_id in _cfg.admin_ids


async def paid_access(user_id: int | None) -> bool:
    if not user_id:
        return False
    if is_superadmin(user_id):
        return True
    return await tenant_db.is_subscription_active(user_id)


async def require_paid(event: Message | CallbackQuery) -> bool:
    user = event.from_user
    await tenant_db.touch_user(user)
    if await paid_access(user.id if user else None):
        return True

    if isinstance(event, CallbackQuery):
        await event.answer(
            "Нужна активная подписка",
            show_alert=True,
        )
        await show_paywall(event.message, user.id)
    else:
        await show_paywall(event, user.id)
    return False


def paywall_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить 1 200 ₽",
                    callback_data="billing:buy",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Применить промокод",
                    callback_data="billing:promo",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Моя подписка",
                    callback_data="billing:status",
                )
            ],
        ]
    )


def cabinet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📢 Мой канал", callback_data="tenant:channels"),
                InlineKeyboardButton(text="📝 Темы", callback_data="tenant:topics"),
            ],
            [
                InlineKeyboardButton(text="⏰ Расписание", callback_data="tenant:schedule"),
                InlineKeyboardButton(text="🔥 Срочные статьи", callback_data="tenant:urgent"),
            ],
            [
                InlineKeyboardButton(text="🧠 Промпты", callback_data="tenant:prompts"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="tenant:stats"),
            ],
            [
                InlineKeyboardButton(text="💬 Дзен", callback_data="tenant:dzen"),
            ],
            [
                InlineKeyboardButton(text="💳 Подписка", callback_data="billing:status"),
            ],
        ]
    )


def dzen_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Указать страницу комментариев",
                    callback_data="tenant:dzen:url",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔐 Авторизовать Дзен",
                    callback_data="tenant:dzen:auth",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Комментарии и ответы",
                    callback_data="tenant:dzen:history:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить статус",
                    callback_data="tenant:dzen",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ В кабинет",
                    callback_data="tenant:home",
                )
            ],
        ]
    )


def channels_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Подключить канал", callback_data="tenant:channel:add")],
            [InlineKeyboardButton(text="⬅️ В кабинет", callback_data="tenant:home")],
        ]
    )


def topics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить темы", callback_data="tenant:topics:add")],
            [
                InlineKeyboardButton(text="🟢 Новые", callback_data="tenant:topics:list:unused"),
                InlineKeyboardButton(text="✅ Использованные", callback_data="tenant:topics:list:used"),
            ],
            [InlineKeyboardButton(text="⬅️ В кабинет", callback_data="tenant:home")],
        ]
    )


def topic_actions(topic_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить",
                    callback_data=f"tenant:topic:edit:{topic_id}",
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"tenant:topic:delete:{topic_id}",
                ),
            ]
        ]
    )


def schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить время", callback_data="tenant:schedule:add")],
            [InlineKeyboardButton(text="▶️/⏸ Автопубликация", callback_data="tenant:schedule:toggle")],
            [InlineKeyboardButton(text="⬅️ В кабинет", callback_data="tenant:home")],
        ]
    )



def urgent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Добавить приоритетные темы",
                    callback_data="tenant:priority:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Приоритетная очередь",
                    callback_data="tenant:priority:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Случайную опубликовать сейчас",
                    callback_data="tenant:urgent:random",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Свою тему опубликовать сейчас",
                    callback_data="tenant:urgent:manual",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ В кабинет",
                    callback_data="tenant:home",
                )
            ],
        ]
    )

def prompts_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Текст статьи", callback_data="tenant:prompt:view:article")],
            [InlineKeyboardButton(text="🖼 Изображение", callback_data="tenant:prompt:view:image")],
            [InlineKeyboardButton(text="⬅️ В кабинет", callback_data="tenant:home")],
        ]
    )


def prompt_actions(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"tenant:prompt:edit:{kind}")],
            [InlineKeyboardButton(text="♻️ Сбросить", callback_data=f"tenant:prompt:reset:{kind}")],
            [InlineKeyboardButton(text="⬅️ К промптам", callback_data="tenant:prompts")],
        ]
    )


def prompt_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сохранить", callback_data="tenant:prompt:save")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="tenant:prompt:cancel")],
        ]
    )


def super_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Клиенты",
                    callback_data="super:users",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Промокоды",
                    callback_data="super:promos",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 SaaS статистика",
                    callback_data="super:stats",
                )
            ],
        ]
    )




def super_promos_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Создать промокод",
                    callback_data="super:promo:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data="super:promos",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="super:home",
                )
            ],
        ]
    )


async def show_paywall(message: Message, user_id: int | None = None) -> None:
    _, cfg, _ = _deps()
    await message.answer(
        "🤖 <b>Автопубликация статей в ваш Telegram-канал</b>\n\n"
        "После оплаты вы получаете личную админ-панель: канал, темы, "
        "расписание, срочные статьи и собственные промпты.\n\n"
        "Стоимость: <b>1 200 ₽ / 30 дней</b>.",
        parse_mode="HTML",
        reply_markup=paywall_keyboard(),
    )


async def show_paid_start(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    await tenant_db.touch_user(message.from_user)
    if await tenant_db.is_subscription_active(message.from_user.id):
        await show_cabinet(message)
    else:
        await show_paywall(message, message.from_user.id)


async def show_cabinet(message: Message) -> None:
    user_id = message.from_user.id
    if not await paid_access(user_id):
        await show_paywall(message, user_id)
        return
    _, cfg, _ = _deps()
    await tenant_db.ensure_defaults(user_id, cfg.default_publish_times, DEFAULT_TOPICS)
    channels = await tenant_db.list_channels(user_id)
    auto = await tenant_db.auto_publish_enabled(user_id)
    sub = await tenant_db.subscription_info(user_id)
    expiry = sub["expires_at"][:10] if sub and sub["expires_at"] else "—"
    await message.answer(
        "⚙️ <b>Мой кабинет</b>\n\n"
        f"Подписка до: <code>{html.escape(expiry)}</code>\n"
        f"Каналов: <b>{len(channels)}</b>\n"
        f"Автопубликация: {'🟢 включена' if auto else '🔴 выключена'}\n"
        f"Часовой пояс: <code>{html.escape(cfg.timezone)}</code>",
        parse_mode="HTML",
        reply_markup=cabinet_keyboard(),
    )


async def show_superadmin(message: Message) -> None:
    if not is_superadmin(message.from_user.id):
        return
    stats = await tenant_db.platform_stats()
    await message.answer(
        "🛠 <b>Paid Multi-Use</b>\n\n"
        f"Пользователей: {stats['users']}\n"
        f"Активных подписок: {stats['active_subscriptions']}\n"
        f"Подключённых каналов: {stats['channels']}\n"
        f"Платежей: {stats['payments']}\n"
        f"Получено Stars: {stats['stars']} ⭐\n\n"
        "Ручная выдача: <code>/grant USER_ID DAYS</code>\n"
        "Отзыв доступа: <code>/revoke USER_ID</code>",
        parse_mode="HTML",
        reply_markup=super_keyboard(),
    )


@router.message(Command("cabinet"))
async def cmd_cabinet(message: Message, state: FSMContext):
    await state.clear()
    await tenant_db.touch_user(message.from_user)
    await show_cabinet(message)


@router.message(Command("buy"))
async def cmd_buy(message: Message):
    await tenant_db.touch_user(message.from_user)
    await create_payment(message)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message):
    await tenant_db.touch_user(message.from_user)
    await send_subscription_status(message, message.from_user.id)


@router.callback_query(F.data == "tenant:home")
async def cb_tenant_home(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    await state.clear()
    await call.answer()
    await show_cabinet(call.message)


# ---------------- PAYMENT ----------------

async def create_payment(
    message: Message,
    user_id: int | None = None,
) -> None:
    user_id = int(
        user_id
        if user_id is not None
        else message.from_user.id
    )

    bot, _, _ = _deps()

    if user_id == bot.id:
        raise RuntimeError(
            "CloudPayments payer cannot be bot"
        )

    if await tenant_db.is_subscription_active(
        user_id
    ):
        await message.answer(
            "✅ Подписка уже активна.",
            reply_markup=cabinet_keyboard(),
        )
        return

    try:
        order = await create_cloudpayments_order(
            user_id
        )
    except Exception:
        log.exception(
            "Ошибка создания CloudPayments "
            "order: user=%s",
            user_id,
        )

        await message.answer(
            "❌ Сейчас не удалось создать "
            "ссылку на оплату. "
            "Попробуйте немного позже."
        )
        return

    amount_kopecks = int(
        order["amount_kopecks"]
    )

    amount_text = (
        f"{amount_kopecks / 100:,.2f}"
        .replace(",", " ")
        .replace(".00", "")
    )

    promo_code = order.get(
        "promo_code"
    )

    discount_percent = int(
        order.get(
            "discount_percent",
            0,
        )
        or 0
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"💳 Оплатить "
                        f"{amount_text} ₽"
                    ),
                    url=order["url"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Промокод",
                    callback_data="billing:promo",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="billing:status",
                )
            ],
        ]
    )

    if promo_code:
        promo_text = (
            f"\n🎟 Промокод: "
            f"<code>{html.escape(str(promo_code))}</code>"
            f" — скидка <b>{discount_percent}%</b>\n"
            f"Цена со скидкой: "
            f"<b>{amount_text} ₽</b>."
        )
    else:
        promo_text = ""

    await message.answer(
        "💳 <b>Подписка на 30 дней</b>\n\n"
        "Базовая стоимость: <b>1 200 ₽</b>."
        f"{promo_text}\n\n"
        "После успешной оплаты доступ "
        "активируется автоматически.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "billing:buy")
async def cb_buy(call: CallbackQuery):
    await tenant_db.touch_user(call.from_user)
    await call.answer()
    await create_payment(
        call.message,
        call.from_user.id,
    )


@router.callback_query(F.data == "billing:promo")
async def cb_promo(
    call: CallbackQuery,
    state: FSMContext,
):
    await tenant_db.touch_user(
        call.from_user
    )

    await state.set_state(
        PaidStates.waiting_promo_code
    )

    await call.answer()

    await call.message.answer(
        "🎟 <b>Промокод</b>\n\n"
        "Введите промокод одним сообщением.",
        parse_mode="HTML",
    )


@router.message(
    PaidStates.waiting_promo_code
)
async def promo_received(
    message: Message,
    state: FSMContext,
):
    await tenant_db.touch_user(
        message.from_user
    )

    code = (
        message.text or ""
    ).strip()

    result = await apply_user_promo(
        message.from_user.id,
        code,
    )

    await state.clear()

    if not result.get("ok"):
        if (
            result.get("reason")
            == "already_used"
        ):
            text = (
                "❌ Этот промокод уже был "
                "использован вами."
            )
        else:
            text = (
                "❌ Промокод не найден "
                "или больше не действует."
            )

        await message.answer(
            text,
            reply_markup=paywall_keyboard(),
        )
        return

    # -----------------------------------------------------
    # 100% скидка — подписка уже активирована backend'ом
    # -----------------------------------------------------

    if result.get("activated"):
        expiry = str(
            result.get("expires_at")
            or ""
        )

        try:
            dt = datetime.fromisoformat(
                expiry
            )

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            expiry_text = (
                dt.astimezone(
                    ZoneInfo("Europe/Moscow")
                )
                .strftime("%d.%m.%Y %H:%M")
            )

        except Exception:
            expiry_text = expiry or "—"

        await message.answer(
            "🎉 <b>Промокод применён!</b>\n\n"
            f"Код: "
            f"<code>{html.escape(result['code'])}</code>\n"
            "Скидка: <b>100%</b>\n"
            "К оплате: <b>0 ₽</b>\n\n"
            "✅ Подписка активирована "
            "на 30 дней.\n"
            f"Действует до: "
            f"<b>{html.escape(expiry_text)}</b>",
            parse_mode="HTML",
            reply_markup=cabinet_keyboard(),
        )
        return

    # -----------------------------------------------------
    # Обычная скидка
    # -----------------------------------------------------

    final_kopecks = int(
        result["final_kopecks"]
    )

    final_text = (
        f"{final_kopecks / 100:,.2f}"
        .replace(",", " ")
        .replace(".00", "")
    )

    await message.answer(
        "✅ <b>Промокод применён.</b>\n\n"
        f"Код: "
        f"<code>{html.escape(result['code'])}</code>\n"
        f"Скидка: "
        f"<b>{result['discount_percent']}%</b>\n"
        f"Новая цена: "
        f"<b>{final_text} ₽</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            f"💳 Оплатить "
                            f"{final_text} ₽"
                        ),
                        callback_data=(
                            "billing:buy"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Убрать промокод",
                        callback_data=(
                            "billing:promo:clear"
                        ),
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    F.data == "billing:promo:clear"
)
async def cb_promo_clear(
    call: CallbackQuery,
    state: FSMContext,
):
    await clear_user_promo(
        call.from_user.id
    )

    await state.clear()
    await call.answer(
        "Промокод удалён"
    )

    await call.message.answer(
        "🎟 Промокод удалён. "
        "Стоимость снова 1 200 ₽.",
        reply_markup=paywall_keyboard(),
    )


async def send_subscription_status(message: Message, user_id: int) -> None:
    row = await tenant_db.subscription_info(user_id)
    active = await tenant_db.is_subscription_active(user_id)
    if not row:
        await message.answer(
            "💳 Подписка пока не оформлена.",
            reply_markup=paywall_keyboard(),
        )
        return
    expiry = row["expires_at"] or "—"
    await message.answer(
        "💳 <b>Подписка</b>\n\n"
        f"Статус: {'🟢 активна' if active else '🔴 неактивна'}\n"
        f"Действует до: <code>{html.escape(expiry)}</code>\n"
        f"Источник: <code>{html.escape(row['source'])}</code>\n"
        f"Автопродление: {'да' if row['is_recurring'] else 'нет'}",
        parse_mode="HTML",
        reply_markup=cabinet_keyboard() if active else paywall_keyboard(),
    )


@router.callback_query(F.data == "billing:status")
async def cb_billing_status(call: CallbackQuery):
    await tenant_db.touch_user(call.from_user)
    await call.answer()
    await send_subscription_status(call.message, call.from_user.id)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await tenant_db.touch_user(query.from_user)
    if query.currency != "XTR" or not query.invoice_payload.startswith("zen-sub:"):
        await query.answer(ok=False, error_message="Некорректный платёж")
        return
    await query.answer(ok=True)


def _payment_expiry(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    _, cfg, _ = _deps()
    await tenant_db.touch_user(message.from_user)
    payment = message.successful_payment
    if payment.currency != "XTR" or not payment.invoice_payload.startswith("zen-sub:"):
        return

    expires = _payment_expiry(getattr(payment, "subscription_expiration_date", None))
    if expires is None:
        expires = datetime.now(timezone.utc) + timedelta(days=30)

    is_recurring = bool(getattr(payment, "is_recurring", False))
    is_first = bool(getattr(payment, "is_first_recurring", False))

    await tenant_db.record_payment(
        user_id=message.from_user.id,
        payload=payment.invoice_payload,
        currency=payment.currency,
        total_amount=payment.total_amount,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        provider_payment_charge_id=getattr(payment, "provider_payment_charge_id", None),
        expires_at=expires,
        is_recurring=is_recurring,
        is_first_recurring=is_first,
    )
    await tenant_db.activate_subscription(
        message.from_user.id,
        expires_at=expires,
        source="telegram_stars",
        stars_amount=payment.total_amount,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        is_recurring=is_recurring,
    )
    await tenant_db.ensure_defaults(
        message.from_user.id,
        cfg.default_publish_times,
        DEFAULT_TOPICS,
    )
    await message.answer(
        "✅ <b>Оплата получена. Доступ активирован.</b>\n\n"
        "Теперь подключите канал и настройте темы/расписание.",
        parse_mode="HTML",
        reply_markup=cabinet_keyboard(),
    )


# ---------------- DZEN TENANT ----------------

async def show_tenant_dzen(
    message: Message,
    user_id: int,
) -> None:
    _, cfg, _ = _deps()

    row = await tenant_db.tenant_dzen_account(
        user_id
    )

    used = await tenant_db.successful_dzen_replies_today(
        user_id,
        cfg.timezone,
    )

    remaining = max(0, 3 - used)

    if row:
        comments_url = str(
            row["comments_url"] or ""
        ).strip()

        enabled = bool(
            row["enabled"]
        )
    else:
        comments_url = ""
        enabled = False

    url_text = (
        html.escape(comments_url)
        if comments_url
        else "не указана"
    )

    await message.answer(
        "💬 <b>Дзен — автоответы</b>\n\n"
        f"Страница комментариев: "
        f"<code>{url_text}</code>\n\n"
        f"Ответов сегодня: <b>{used}/3</b>\n"
        f"Осталось сегодня: <b>{remaining}</b>\n\n"
        f"Автоответы: "
        f"{'🟢 включены' if enabled else '⚪ пока не включены'}\n\n"
        "В подписку входит до "
        "<b>3 подтверждённых ответов в сутки</b>.\n"
        "Неудачные попытки и пропущенные комментарии "
        "лимит не расходуют.\n\n"
        "Сначала укажите страницу комментариев Дзена. "
        "Авторизацию аккаунта подключим отдельным шагом.",
        parse_mode="HTML",
        reply_markup=dzen_keyboard(),
    )


@router.callback_query(F.data == "tenant:dzen")
async def cb_tenant_dzen(
    call: CallbackQuery,
    state: FSMContext,
):
    if not await require_paid(call):
        return

    await state.clear()
    await call.answer()

    await tenant_db.ensure_tenant_dzen_account(
        call.from_user.id
    )

    await show_tenant_dzen(
        call.message,
        call.from_user.id,
    )



def _dzen_history_cut(
    value: Any,
    limit: int,
) -> str:
    text = " ".join(
        str(value or "").split()
    ).strip()

    if len(text) <= limit:
        return text

    return (
        text[: max(1, limit - 1)]
        .rstrip()
        + "…"
    )


def _dzen_history_time(
    value: Any,
    timezone_name: str,
) -> str:
    raw = str(value or "").strip()

    if not raw:
        return "время неизвестно"

    try:
        dt = datetime.fromisoformat(raw)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        try:
            tz = ZoneInfo(
                timezone_name
            )
        except Exception:
            tz = timezone.utc

        return (
            dt.astimezone(tz)
            .strftime("%d.%m.%Y %H:%M")
        )

    except Exception:
        return raw[:40]


async def build_dzen_history(
    user_id: int,
    page: int,
) -> tuple[
    str,
    InlineKeyboardMarkup,
]:
    _, cfg, _ = _deps()

    row = await tenant_db.tenant_dzen_account(
        user_id
    )

    if not row:
        return (
            "💬 <b>Комментарии и ответы</b>\n\n"
            "Аккаунт Дзена пока не подключён.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ К Дзену",
                            callback_data="tenant:dzen",
                        )
                    ]
                ]
            ),
        )

    state_file = str(
        row["state_file"] or ""
    ).strip()

    if not state_file:
        return (
            "💬 <b>Комментарии и ответы</b>\n\n"
            "История пока отсутствует.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ К Дзену",
                            callback_data="tenant:dzen",
                        )
                    ]
                ]
            ),
        )

    try:
        data = json.loads(
            Path(state_file).read_text(
                encoding="utf-8"
            )
        )
    except FileNotFoundError:
        data = {}
    except Exception:
        log.exception(
            "Не удалось прочитать Dzen "
            "history: user=%s file=%s",
            user_id,
            state_file,
        )
        data = {}

    processed = data.get(
        "processed",
        {},
    )

    if not isinstance(
        processed,
        dict,
    ):
        processed = {}

    rows: list[dict[str, Any]] = []

    for item in processed.values():
        if not isinstance(
            item,
            dict,
        ):
            continue

        status = str(
            item.get("status") or ""
        ).strip()

        reply = str(
            item.get("reply") or ""
        ).strip()

        # Показываем только случаи,
        # когда бот действительно
        # подготовил и отправлял ответ.
        if (
            status
            not in {
                "replied",
                "replied_unverified",
            }
            or not reply
        ):
            continue

        rows.append(
            {
                "author":
                    item.get("author"),
                "comment":
                    item.get("comment"),
                "article_title":
                    item.get(
                        "article_title"
                    ),
                "reply":
                    reply,
                "status":
                    status,
                "at":
                    item.get("at"),
            }
        )

    rows.sort(
        key=lambda item: str(
            item.get("at") or ""
        ),
        reverse=True,
    )

    if not rows:
        return (
            "💬 <b>Комментарии и ответы</b>\n\n"
            "Бот пока не отвечал "
            "на комментарии.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔄 Обновить",
                            callback_data=(
                                "tenant:dzen:"
                                "history:0"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ К Дзену",
                            callback_data="tenant:dzen",
                        )
                    ],
                ]
            ),
        )

    per_page = 2

    total = len(rows)

    total_pages = max(
        1,
        (
            total
            + per_page
            - 1
        )
        // per_page,
    )

    page = max(
        0,
        min(
            int(page),
            total_pages - 1,
        ),
    )

    start = page * per_page
    end = min(
        start + per_page,
        total,
    )

    current = rows[start:end]

    lines = [
        "💬 <b>Комментарии и ответы Дзена</b>",
        "",
        (
            f"Всего ответов в истории: "
            f"<b>{total}</b>"
        ),
        (
            f"Страница: "
            f"<b>{page + 1}/{total_pages}</b>"
        ),
        "",
    ]

    for index, item in enumerate(
        current,
        start=start + 1,
    ):
        author = (
            _dzen_history_cut(
                item["author"],
                80,
            )
            or "Автор не указан"
        )

        article = _dzen_history_cut(
            item["article_title"],
            150,
        )

        comment = (
            _dzen_history_cut(
                item["comment"],
                500,
            )
            or "—"
        )

        reply = _dzen_history_cut(
            item["reply"],
            700,
        )

        time_text = _dzen_history_time(
            item["at"],
            cfg.timezone,
        )

        if (
            item["status"]
            == "replied"
        ):
            status_text = (
                "✅ публикация подтверждена"
            )
        else:
            status_text = (
                "⚠️ отправлен, "
                "подтверждение не получено"
            )

        lines.append(
            f"<b>{index}. "
            f"{html.escape(author)}</b>"
        )

        if article:
            lines.append(
                "📰 <b>Статья:</b> "
                f"{html.escape(article)}"
            )

        lines.extend(
            [
                "",
                "💭 <b>Комментарий:</b>",
                html.escape(comment),
                "",
                "🤖 <b>Ответ бота:</b>",
                html.escape(reply),
                "",
                (
                    f"🕒 {html.escape(time_text)}"
                    f" · {status_text}"
                ),
                "",
                "──────────────",
                "",
            ]
        )

    buttons: list[
        list[InlineKeyboardButton]
    ] = []

    navigation: list[
        InlineKeyboardButton
    ] = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Предыдущие",
                callback_data=(
                    "tenant:dzen:history:"
                    f"{page - 1}"
                ),
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                text="Следующие ➡️",
                callback_data=(
                    "tenant:dzen:history:"
                    f"{page + 1}"
                ),
            )
        )

    if navigation:
        buttons.append(
            navigation
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=(
                    "tenant:dzen:history:"
                    f"{page}"
                ),
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ К Дзену",
                callback_data="tenant:dzen",
            )
        ]
    )

    return (
        "\n".join(lines).strip(),
        InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


@router.callback_query(
    F.data.startswith(
        "tenant:dzen:history:"
    )
)
async def cb_tenant_dzen_history(
    call: CallbackQuery,
):
    if not await require_paid(call):
        return

    try:
        page = int(
            call.data.rsplit(
                ":",
                1,
            )[1]
        )
    except Exception:
        page = 0

    text, keyboard = (
        await build_dzen_history(
            call.from_user.id,
            page,
        )
    )

    await call.answer()

    try:
        await call.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        # Например, если Telegram
        # считает текст неизменившимся.
        await call.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


@router.callback_query(F.data == "tenant:dzen:auth")
async def cb_tenant_dzen_auth(
    call: CallbackQuery,
    state: FSMContext,
):
    if not await require_paid(call):
        return

    row = await tenant_db.tenant_dzen_account(
        call.from_user.id
    )

    if not row:
        await call.answer(
            "Сначала укажите страницу комментариев",
            show_alert=True,
        )
        return

    comments_url = str(
        row["comments_url"] or ""
    ).strip()

    if not comments_url:
        await call.answer(
            "Сначала укажите страницу комментариев",
            show_alert=True,
        )
        return

    await state.set_state(
        PaidStates.waiting_dzen_login
    )

    await call.answer()

    await call.message.answer(
        "🔐 <b>Авторизация Дзена</b>\n\n"
        "Пришлите только логин Яндекса, "
        "к которому привязан ваш Дзен.\n\n"
        "Например:\n"
        "<code>example@yandex.ru</code>\n\n"
        "⚠️ Пароль, код из SMS или код 2FA "
        "сюда отправлять не нужно.",
        parse_mode="HTML",
    )


@router.message(PaidStates.waiting_dzen_login)
async def tenant_dzen_login_received(
    message: Message,
    state: FSMContext,
):
    if not await require_paid(message):
        return

    login = (message.text or "").strip()

    if (
        len(login) < 3
        or len(login) > 254
        or any(ch.isspace() for ch in login)
    ):
        await message.answer(
            "❌ Некорректный логин Яндекса.\n"
            "Пришлите логин или email без пробелов."
        )
        return

    row = await tenant_db.tenant_dzen_account(
        message.from_user.id
    )

    if not row:
        await state.clear()

        await message.answer(
            "❌ Сначала укажите страницу комментариев Дзена.",
            reply_markup=dzen_keyboard(),
        )
        return

    comments_url = str(
        row["comments_url"] or ""
    ).strip()

    profile_dir = str(
        row["profile_dir"] or ""
    ).strip()

    if not comments_url or not profile_dir:
        await state.clear()

        await message.answer(
            "❌ Настройки Дзена неполные. "
            "Сначала заново укажите страницу комментариев.",
            reply_markup=dzen_keyboard(),
        )
        return

    # Пока идёт авторизация, worker клиента
    # не должен использовать этот профиль.
    await tenant_db.set_tenant_dzen_enabled(
        message.from_user.id,
        False,
    )

    await state.clear()

    bot, _, _ = _deps()

    await message.answer(
        "🔄 Готовлю QR-код для входа в Яндекс…\n\n"
        "Пароль вводить в Telegram не потребуется."
    )

    task = asyncio.create_task(
        authorize_tenant_dzen_by_qr(
            bot=bot,
            user_id=message.from_user.id,
            yandex_login=login,
            comments_url=comments_url,
            profile_dir=profile_dir,
            timeout_seconds=180,
        )
    )

    _dzen_auth_tasks.add(task)

    task.add_done_callback(
        _dzen_auth_tasks.discard
    )


@router.callback_query(F.data == "tenant:dzen:url")
async def cb_tenant_dzen_url(
    call: CallbackQuery,
    state: FSMContext,
):
    if not await require_paid(call):
        return

    await state.set_state(
        PaidStates.waiting_dzen_comments_url
    )

    await call.answer()

    await call.message.answer(
        "Пришлите ссылку на страницу "
        "комментариев в редакторе Дзена.\n\n"
        "Пример:\n"
        "<code>https://dzen.ru/profile/editor/"
        "id/.../comments/</code>",
        parse_mode="HTML",
    )


@router.message(PaidStates.waiting_dzen_comments_url)
async def tenant_dzen_url_received(
    message: Message,
    state: FSMContext,
):
    if not await require_paid(message):
        return

    url = (message.text or "").strip()

    if (
        not url.startswith("https://dzen.ru/")
        or "/comments" not in url
    ):
        await message.answer(
            "❌ Это не похоже на страницу "
            "комментариев Дзена.\n\n"
            "Нужна ссылка вида:\n"
            "<code>https://dzen.ru/profile/editor/"
            "id/.../comments/</code>",
            parse_mode="HTML",
        )
        return

    await tenant_db.ensure_tenant_dzen_account(
        message.from_user.id
    )

    await tenant_db.set_tenant_dzen_comments_url(
        message.from_user.id,
        url,
    )

    # Пока НЕ включаем worker автоматически:
    # сначала нужна отдельная авторизация
    # Dzen-профиля этого пользователя.
    await tenant_db.set_tenant_dzen_enabled(
        message.from_user.id,
        False,
    )

    await state.clear()

    await message.answer(
        "✅ Страница комментариев сохранена.\n\n"
        "Следующий этап — авторизация "
        "вашего аккаунта Дзена.",
        reply_markup=dzen_keyboard(),
    )

    await show_tenant_dzen(
        message,
        message.from_user.id,
    )


# ---------------- CHANNELS ----------------

@router.callback_query(F.data == "tenant:channels")
async def cb_channels(call: CallbackQuery):
    if not await require_paid(call):
        return
    await call.answer()
    rows = await tenant_db.list_channels(call.from_user.id)
    await call.message.answer(
        "📢 <b>Мои каналы</b>\n\n"
        "Для публикации бот должен быть администратором канала с правом публикации сообщений.",
        parse_mode="HTML",
        reply_markup=channels_keyboard(),
    )
    if not rows:
        await call.message.answer("Подключённых каналов пока нет.")
        return
    for row in rows:
        username = f" @{row['username']}" if row["username"] else ""
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Отключить", callback_data=f"tenant:channel:remove:{row['id']}")]
            ]
        )
        await call.message.answer(
            f"✅ {html.escape(row['title'])}{html.escape(username)}\n"
            f"<code>{row['chat_id']}</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )


@router.callback_query(F.data == "tenant:channel:add")
async def cb_channel_add(call: CallbackQuery):
    if not await require_paid(call):
        return
    bot, cfg, _ = _deps()
    count = await tenant_db.channel_count(call.from_user.id)
    if count >= cfg.tenant_channel_limit:
        await call.answer("Лимит каналов исчерпан", show_alert=True)
        return
    me = await bot.get_me()
    await call.answer()
    request = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📢 Выбрать канал",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=4901,
                        chat_is_channel=True,
                        bot_is_member=True,
                        request_title=True,
                        request_username=True,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите канал",
    )
    await call.message.answer(
        "Сначала добавьте бота "
        f"@{me.username} в нужный канал <b>администратором</b> и включите право "
        "<b>публиковать сообщения</b>.\n\n"
        "После этого нажмите кнопку ниже и выберите канал.",
        parse_mode="HTML",
        reply_markup=request,
    )


@router.message(F.chat_shared)
async def channel_shared(message: Message):
    if not await require_paid(message):
        return
    bot, cfg, _ = _deps()
    shared = message.chat_shared
    if shared.request_id != 4901:
        return
    chat_id = int(shared.chat_id)

    try:
        bot_member = await bot.get_chat_member(chat_id, bot.id)
        if bot_member.status not in {"administrator", "creator"}:
            raise RuntimeError("бот не администратор")
        if bot_member.status == "administrator" and not bool(
            getattr(bot_member, "can_post_messages", False)
        ):
            raise RuntimeError("у бота нет права can_post_messages")

        user_member = await bot.get_chat_member(chat_id, message.from_user.id)
        if user_member.status not in {"administrator", "creator"}:
            raise RuntimeError("пользователь не администратор этого канала")

        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        await message.answer(
            "❌ Канал не подключён. Сделайте бота администратором канала с правом "
            f"публикации и повторите выбор.\n\nПричина: {html.escape(str(exc))}",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if await tenant_db.channel_count(message.from_user.id) >= cfg.tenant_channel_limit:
        await message.answer(
            "❌ Достигнут лимит каналов для тарифа.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    result = await tenant_db.connect_channel(
        message.from_user.id,
        chat_id=chat_id,
        title=chat.title or getattr(shared, "title", None) or str(chat_id),
        username=getattr(chat, "username", None) or getattr(shared, "username", None),
    )
    if result == "owned_by_other":
        await message.answer(
            "❌ Этот канал уже привязан к другому аккаунту в боте.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await message.answer(
        f"✅ Канал <b>{html.escape(chat.title or str(chat_id))}</b> подключён.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await show_cabinet(message)


@router.callback_query(F.data.startswith("tenant:channel:remove:"))
async def cb_channel_remove(call: CallbackQuery):
    if not await require_paid(call):
        return
    channel_id = int(call.data.rsplit(":", 1)[1])
    ok = await tenant_db.remove_channel(call.from_user.id, channel_id)
    await call.answer("Отключено" if ok else "Канал не найден")
    if ok:
        await call.message.edit_text("🗑 Канал отключён от автопубликации.")


# ---------------- TOPICS ----------------

@router.callback_query(F.data == "tenant:topics")
async def cb_topics(call: CallbackQuery):
    if not await require_paid(call):
        return
    await call.answer()
    await call.message.answer(
        "📝 <b>Мои темы</b>\n\n"
        "Тема не повторяется два раза подряд. После прохождения списка старые темы могут использоваться снова.",
        parse_mode="HTML",
        reply_markup=topics_keyboard(),
    )


@router.callback_query(F.data == "tenant:topics:add")
async def cb_topics_add(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    await state.set_state(PaidStates.waiting_topics)
    await call.answer()
    await call.message.answer("Пришлите темы. Каждая новая строка = отдельная тема.")


@router.message(PaidStates.waiting_topics)
async def topics_add(message: Message, state: FSMContext):
    if not await require_paid(message):
        return
    count = await tenant_db.add_topics(message.from_user.id, (message.text or "").splitlines())
    await state.clear()
    await message.answer(f"✅ Добавлено новых тем: {count}", reply_markup=topics_keyboard())


@router.callback_query(F.data.startswith("tenant:topics:list:"))
async def cb_topics_list(call: CallbackQuery):
    if not await require_paid(call):
        return
    mode = call.data.rsplit(":", 1)[1]
    rows = await tenant_db.list_topics(call.from_user.id, mode=mode, limit=50)
    await call.answer()
    if not rows:
        await call.message.answer("Список пуст.", reply_markup=topics_keyboard())
        return
    await call.message.answer(f"📋 Тем: {len(rows)}")
    for row in rows:
        used = f"\nПоследнее использование: {row['used_at']}" if row["used_at"] else ""
        await call.message.answer(
            f"#{row['id']} — {html.escape(row['title'])}{used}",
            parse_mode="HTML",
            reply_markup=topic_actions(int(row["id"])),
        )


@router.callback_query(F.data.startswith("tenant:topic:edit:"))
async def cb_topic_edit(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    topic_id = int(call.data.rsplit(":", 1)[1])
    row = await tenant_db.get_topic(call.from_user.id, topic_id)
    if not row:
        await call.answer("Тема не найдена", show_alert=True)
        return
    await state.update_data(tenant_topic_id=topic_id)
    await state.set_state(PaidStates.waiting_edit_topic)
    await call.answer()
    await call.message.answer(f"Текущая тема:\n{row['title']}\n\nПришлите новое название:")


@router.message(PaidStates.waiting_edit_topic)
async def topic_edit(message: Message, state: FSMContext):
    if not await require_paid(message):
        return
    data = await state.get_data()
    ok = await tenant_db.update_topic(
        message.from_user.id,
        int(data["tenant_topic_id"]),
        message.text or "",
    )
    await state.clear()
    await message.answer(
        "✅ Тема изменена." if ok else "❌ Не удалось изменить тему.",
        reply_markup=topics_keyboard(),
    )


@router.callback_query(F.data.startswith("tenant:topic:delete:"))
async def cb_topic_delete(call: CallbackQuery):
    if not await require_paid(call):
        return
    topic_id = int(call.data.rsplit(":", 1)[1])
    ok = await tenant_db.deactivate_topic(call.from_user.id, topic_id)
    await call.answer("Удалено" if ok else "Не найдено")
    if ok:
        await call.message.edit_text("🗑 Тема удалена из активного списка.")


# ---------------- SCHEDULE ----------------

async def send_schedule(message: Message, user_id: int) -> None:
    _, cfg, _ = _deps()
    rows = await tenant_db.list_schedule(user_id)
    auto = await tenant_db.auto_publish_enabled(user_id)
    await message.answer(
        "⏰ <b>Моё расписание</b>\n\n"
        f"Статус: {'🟢 включено' if auto else '🔴 выключено'}\n"
        f"Часовой пояс: <code>{html.escape(cfg.timezone)}</code>",
        parse_mode="HTML",
        reply_markup=schedule_keyboard(),
    )
    for row in rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"tenant:schedule:delete:{row['id']}")]
            ]
        )
        await message.answer(f"🕒 {row['publish_time']}", reply_markup=kb)


@router.callback_query(F.data == "tenant:schedule")
async def cb_schedule(call: CallbackQuery):
    if not await require_paid(call):
        return
    await call.answer()
    await send_schedule(call.message, call.from_user.id)


@router.callback_query(F.data == "tenant:schedule:add")
async def cb_schedule_add(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    await state.set_state(PaidStates.waiting_schedule_time)
    await call.answer()
    await call.message.answer("Введите время ЧЧ:ММ, например 11:30")


@router.message(PaidStates.waiting_schedule_time)
async def schedule_add(message: Message, state: FSMContext):
    if not await require_paid(message):
        return
    ok = await tenant_db.add_schedule_time(message.from_user.id, (message.text or "").strip())
    if not ok:
        await message.answer("❌ Неверное или уже существующее время. Например: 14:00")
        return
    await state.clear()
    await send_schedule(message, message.from_user.id)


@router.callback_query(F.data.startswith("tenant:schedule:delete:"))
async def cb_schedule_delete(call: CallbackQuery):
    if not await require_paid(call):
        return
    schedule_id = int(call.data.rsplit(":", 1)[1])
    await tenant_db.delete_schedule_time(call.from_user.id, schedule_id)
    await call.answer("Удалено")
    await call.message.edit_text("🗑 Время удалено.")


@router.callback_query(F.data == "tenant:schedule:toggle")
async def cb_schedule_toggle(call: CallbackQuery):
    if not await require_paid(call):
        return
    current = await tenant_db.auto_publish_enabled(call.from_user.id)
    await tenant_db.set_auto_publish_enabled(call.from_user.id, not current)
    await call.answer("Изменено")
    await send_schedule(call.message, call.from_user.id)


# ---------------- URGENT ----------------

@router.callback_query(F.data == "tenant:urgent")
async def cb_urgent(call: CallbackQuery):
    if not await require_paid(call):
        return
    await call.answer()
    await call.message.answer(
        "⚡ Срочная статья публикуется сразу и не ждёт расписания. "
        "Тема, введённая вручную, не сохраняется в общий пул.",
        reply_markup=urgent_keyboard(),
    )


def result_text(result: dict) -> str:
    status = result.get("status")
    if status == "ok":
        return (
            "✅ Статья опубликована.\n\n"
            f"Тема: {result.get('topic')}\n"
            f"Заголовок: {result.get('article_title')}\n"
            f"Каналов: {result.get('channels_published', 0)}"
        )
    mapping = {
        "no_channel": "⚠️ Сначала подключите канал.",
        "no_topics": "⚠️ Нет активных тем.",
        "subscription_inactive": "⛔ Подписка неактивна.",
    }
    return mapping.get(status, "❌ Ошибка: " + str(result.get("error", status)))



@router.callback_query(
    F.data == "tenant:priority:add"
)
async def cb_tenant_priority_add(
    call: CallbackQuery,
    state: FSMContext,
):
    if not await require_paid(call):
        return

    await state.set_state(
        PaidStates.waiting_priority_topics
    )

    await call.answer()

    await call.message.answer(
        "🔥 <b>Добавить приоритетные темы</b>\n\n"
        "Отправьте темы одним сообщением.\n"
        "Каждая новая строка = отдельная тема.\n\n"
        "Они не публикуются сразу. "
        "Бот поставит их впереди обычной очереди "
        "и будет публиковать по вашему расписанию.",
        parse_mode="HTML",
    )


@router.message(
    PaidStates.waiting_priority_topics
)
async def tenant_priority_topics_received(
    message: Message,
    state: FSMContext,
):
    if not await require_paid(message):
        await state.clear()
        return

    lines = (
        message.text or ""
    ).splitlines()

    count = (
        await tenant_db.add_priority_topics(
            message.from_user.id,
            lines,
        )
    )

    await state.clear()

    await message.answer(
        "✅ <b>Приоритетная очередь обновлена.</b>\n\n"
        f"Добавлено/поднято в приоритет: "
        f"<b>{count}</b>\n\n"
        "Следующая плановая публикация сначала "
        "возьмёт тему из этой очереди.",
        parse_mode="HTML",
        reply_markup=urgent_keyboard(),
    )


@router.callback_query(
    F.data == "tenant:priority:list"
)
async def cb_tenant_priority_list(
    call: CallbackQuery,
):
    if not await require_paid(call):
        return

    await call.answer()

    rows = (
        await tenant_db.list_priority_topics(
            call.from_user.id,
            limit=50,
        )
    )

    if not rows:
        await call.message.answer(
            "📋 Приоритетная очередь пуста.",
            reply_markup=urgent_keyboard(),
        )
        return

    await call.message.answer(
        "🔥 <b>Приоритетная очередь</b>\n\n"
        f"Тем: <b>{len(rows)}</b>\n"
        "Бот обработает их раньше обычных тем "
        "в плановые часы публикации.",
        parse_mode="HTML",
    )

    for number, row in enumerate(
        rows,
        start=1,
    ):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↩️ Снять приоритет",
                        callback_data=(
                            "tenant:priority:remove:"
                            f"{row['id']}"
                        ),
                    )
                ]
            ]
        )

        await call.message.answer(
            f"<b>{number}.</b> "
            f"{html.escape(str(row['title']))}",
            parse_mode="HTML",
            reply_markup=kb,
        )


@router.callback_query(
    F.data.startswith(
        "tenant:priority:remove:"
    )
)
async def cb_tenant_priority_remove(
    call: CallbackQuery,
):
    if not await require_paid(call):
        return

    try:
        topic_id = int(
            call.data.rsplit(
                ":",
                1,
            )[1]
        )
    except Exception:
        await call.answer(
            "Некорректная тема",
            show_alert=True,
        )
        return

    ok = (
        await tenant_db.clear_topic_priority(
            call.from_user.id,
            topic_id,
        )
    )

    await call.answer(
        "Приоритет снят"
        if ok
        else "Тема не найдена"
    )

    if ok:
        await call.message.edit_text(
            "↩️ Приоритет снят.\n\n"
            "Тема остаётся в обычной очереди."
        )


@router.callback_query(F.data == "tenant:urgent:random")
async def cb_urgent_random(call: CallbackQuery):
    if not await require_paid(call):
        return
    _, _, service = _deps()
    await call.answer()
    status = await call.message.answer("⚡ Создаю статью…")
    result = await service.publish_random_topic(call.from_user.id, trigger="urgent_random")
    await status.edit_text(result_text(result))


@router.callback_query(F.data == "tenant:urgent:manual")
async def cb_urgent_manual(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    await state.set_state(PaidStates.waiting_urgent_topic)
    await call.answer()
    await call.message.answer("Введите тему срочной статьи:")


@router.message(PaidStates.waiting_urgent_topic)
async def urgent_manual(message: Message, state: FSMContext):
    if not await require_paid(message):
        return
    _, _, service = _deps()
    topic = " ".join((message.text or "").split())
    await state.clear()
    status = await message.answer(f"⚡ Создаю статью по теме:\n{topic}")
    result = await service.publish_manual_topic(message.from_user.id, topic)
    await status.edit_text(result_text(result))


# ---------------- PROMPTS ----------------

async def effective_prompt(user_id: int, kind: str) -> tuple[str, bool]:
    meta = PROMPTS[kind]
    custom = (await tenant_db.get_setting(user_id, meta["key"], "")).strip()
    return (custom, True) if custom else (meta["default"], False)


@router.callback_query(F.data == "tenant:prompts")
async def cb_prompts(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    await state.clear()
    await call.answer()
    await call.message.answer(
        "🧠 <b>Мои промпты</b>\n\n"
        "Промпт текста и изображения индивидуальны для вашего аккаунта. "
        "Изменение применяется со следующей статьи.\n\n"
        "Для изображения используйте <code>{topic}</code> — сюда подставляется текущая тема.",
        parse_mode="HTML",
        reply_markup=prompts_keyboard(),
    )


@router.callback_query(F.data.startswith("tenant:prompt:view:"))
async def cb_prompt_view(call: CallbackQuery):
    if not await require_paid(call):
        return
    kind = call.data.rsplit(":", 1)[1]
    if kind not in PROMPTS:
        await call.answer("Неизвестный промпт", show_alert=True)
        return
    prompt, custom = await effective_prompt(call.from_user.id, kind)
    meta = PROMPTS[kind]
    await call.answer()
    await call.message.answer(
        f"{meta['title']}\n\n"
        f"Версия: {'🟢 пользовательская' if custom else '⚪ стандартная'}\n"
        f"Длина: {len(prompt)} символов.",
        reply_markup=prompt_actions(kind),
    )
    await call.message.answer_document(
        BufferedInputFile(prompt.encode("utf-8"), filename=meta["filename"]),
        caption="Текущий активный промпт",
    )


@router.callback_query(F.data.startswith("tenant:prompt:edit:"))
async def cb_prompt_edit(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    kind = call.data.rsplit(":", 1)[1]
    if kind not in PROMPTS:
        await call.answer("Неизвестный промпт", show_alert=True)
        return
    await state.clear()
    await state.update_data(tenant_prompt_kind=kind, tenant_prompt_parts=[])
    await state.set_state(PaidStates.waiting_prompt_parts)
    await call.answer()
    extra = "\n\nДля картинки желательно оставить {topic}." if kind == "image" else ""
    await call.message.answer(
        "Отправляйте новый промпт одним или несколькими сообщениями. "
        "Когда закончите — нажмите «✅ Сохранить»." + extra,
        reply_markup=prompt_edit_keyboard(),
    )


@router.message(PaidStates.waiting_prompt_parts)
async def prompt_part(message: Message, state: FSMContext):
    if not await require_paid(message):
        return
    part = message.text or ""
    if not part.strip():
        return
    data = await state.get_data()
    parts = list(data.get("tenant_prompt_parts", []))
    parts.append(part)
    await state.update_data(tenant_prompt_parts=parts)
    await message.answer(
        f"✅ Фрагмент добавлен. Всего: {len(parts)}.",
        reply_markup=prompt_edit_keyboard(),
    )


@router.callback_query(F.data == "tenant:prompt:save")
async def cb_prompt_save(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    data = await state.get_data()
    kind = data.get("tenant_prompt_kind")
    parts = data.get("tenant_prompt_parts", [])
    if kind not in PROMPTS:
        await call.answer("Редактор не активен", show_alert=True)
        return
    prompt = "\n\n".join(str(x).strip() for x in parts if str(x).strip()).strip()
    if not prompt:
        await call.answer("Сначала пришлите текст", show_alert=True)
        return
    await tenant_db.set_setting(call.from_user.id, PROMPTS[kind]["key"], prompt)
    await state.clear()
    await call.answer("Сохранено")
    await call.message.answer(
        f"✅ {PROMPTS[kind]['title']} обновлён.",
        reply_markup=prompts_keyboard(),
    )


@router.callback_query(F.data.startswith("tenant:prompt:reset:"))
async def cb_prompt_reset(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    kind = call.data.rsplit(":", 1)[1]
    if kind not in PROMPTS:
        return
    await tenant_db.set_setting(call.from_user.id, PROMPTS[kind]["key"], "")
    await state.clear()
    await call.answer("Сброшено")
    await call.message.answer("♻️ Возвращён стандартный промпт.", reply_markup=prompts_keyboard())


@router.callback_query(F.data == "tenant:prompt:cancel")
async def cb_prompt_cancel(call: CallbackQuery, state: FSMContext):
    if not await require_paid(call):
        return
    await state.clear()
    await call.answer("Отменено")
    await call.message.answer("Изменения не сохранены.", reply_markup=prompts_keyboard())


# ---------------- STATS ----------------

@router.callback_query(F.data == "tenant:stats")
async def cb_stats(call: CallbackQuery):
    if not await require_paid(call):
        return
    await call.answer()
    stats = await tenant_db.stats(call.from_user.id)
    await call.message.answer(
        "📊 <b>Моя статистика</b>\n\n"
        f"Публикаций: {stats['total']}\n"
        f"Успешно: {stats['published']}\n"
        f"Активных тем: {stats['topics']}\n"
        f"Каналов: {stats['channels']}\n"
        f"Последняя статья: {html.escape(stats['last_title'] or '—')}",
        parse_mode="HTML",
        reply_markup=cabinet_keyboard(),
    )




# =========================================================
# HELP
# =========================================================









@router.message(Command("help"))
async def cmd_help(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    if message.from_user:
        await tenant_db.touch_user(
            message.from_user
        )

    user_id = (
        message.from_user.id
        if message.from_user
        else 0
    )

    await message.answer(
        help_home_text(is_superadmin(user_id)),
        parse_mode="HTML",
        reply_markup=help_keyboard(is_superadmin(user_id)),
    )


@router.callback_query(
    F.data == "help:home"
)
async def cb_help_home(
    call: CallbackQuery,
):
    user_id = call.from_user.id

    await call.answer()

    await call.message.edit_text(
        help_home_text(is_superadmin(user_id)),
        parse_mode="HTML",
        reply_markup=help_keyboard(is_superadmin(user_id)),
    )


@router.callback_query(
    F.data.startswith("help:")
)
async def cb_help_section(
    call: CallbackQuery,
):
    user_id = call.from_user.id

    section = call.data.split(
        ":",
        1,
    )[1]

    allowed = {
        "start",
        "channel",
        "articles",
        "schedule",
        "dzen",
        "prompts",
        "payment",
        "faq",
        "commands",
        "admin",
    }

    if section not in allowed:
        await call.answer(
            "Раздел не найден",
            show_alert=True,
        )
        return

    if (
        section == "admin"
        and not is_superadmin(user_id)
    ):
        await call.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    await call.answer()

    await call.message.edit_text(
        help_section_text(
            section,
            is_superadmin(user_id),
        ),
        parse_mode="HTML",
        reply_markup=help_back_keyboard(),
    )


# ---------------- SUPERADMIN ----------------

@router.message(Command("super"))
async def cmd_super(message: Message):
    if not is_superadmin(message.from_user.id):
        return
    await show_superadmin(message)


@router.message(Command("grant"))
async def cmd_grant(message: Message):
    if not is_superadmin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /grant USER_ID [DAYS]")
        return
    try:
        user_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
    except ValueError:
        await message.answer("USER_ID и DAYS должны быть числами")
        return
    _, cfg, _ = _deps()
    expires = await tenant_db.grant_subscription(user_id, days)
    await tenant_db.ensure_defaults(user_id, cfg.default_publish_times, DEFAULT_TOPICS)
    await message.answer(
        f"✅ Пользователю <code>{user_id}</code> выдан доступ до "
        f"<code>{expires.isoformat(timespec='seconds')}</code>.",
        parse_mode="HTML",
    )
    try:
        await _bot.send_message(user_id, "✅ Вам активирован доступ к боту. Откройте /cabinet")
    except Exception:
        pass


@router.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if not is_superadmin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Использование: /revoke USER_ID")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("USER_ID должен быть числом")
        return
    await tenant_db.revoke_subscription(user_id)
    await message.answer(f"⛔ Доступ пользователя {user_id} отозван.")


@router.callback_query(F.data == "super:stats")
async def cb_super_stats(call: CallbackQuery):
    if not is_superadmin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    await show_superadmin(call.message)


@router.callback_query(F.data == "super:users")
async def cb_super_users(call: CallbackQuery):
    if not is_superadmin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.answer()
    rows = await tenant_db.list_users(30)
    if not rows:
        await call.message.answer("Пользователей пока нет.")
        return
    lines = ["👥 <b>Последние пользователи</b>"]
    for row in rows:
        name = row["username"] or row["first_name"] or "—"
        active = False
        if row["status"] == "active" and row["expires_at"]:
            try:
                active = datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
            except Exception:
                active = False
        lines.append(
            f"{'🟢' if active else '⚪'} <code>{row['user_id']}</code> "
            f"{html.escape(str(name))} — до {html.escape(str(row['expires_at'] or '—'))}"
        )
    await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=super_keyboard())


# ---------------- PROMOCODES ----------------

@router.message(Command("promo"))
async def cmd_promo_create(
    message: Message,
):
    if not is_superadmin(
        message.from_user.id
    ):
        return

    parts = (
        message.text or ""
    ).split()

    if len(parts) != 3:
        await message.answer(
            "Использование:\n"
            "<code>/promo КОД СКИДКА</code>\n\n"
            "Пример:\n"
            "<code>/promo START20 20</code>",
            parse_mode="HTML",
        )
        return

    code = parts[1]

    try:
        percent = int(parts[2])
    except ValueError:
        await message.answer(
            "Скидка должна быть числом."
        )
        return

    try:
        promo = await create_promo_code(
            code,
            percent,
        )
    except ValueError as exc:
        await message.answer(
            f"❌ {html.escape(str(exc))}"
        )
        return

    final = int(
        promo["final_kopecks"]
    )

    final_text = (
        f"{final / 100:,.2f}"
        .replace(",", " ")
        .replace(".00", "")
    )

    await message.answer(
        "✅ <b>Промокод создан.</b>\n\n"
        f"Код: <code>{html.escape(promo['code'])}</code>\n"
        f"Скидка: <b>{promo['discount_percent']}%</b>\n"
        f"Цена: <b>{final_text} ₽</b>\n\n"
        "Один пользователь может "
        "использовать этот код один раз.",
        parse_mode="HTML",
    )


@router.message(Command("promos"))
async def cmd_promos(
    message: Message,
):
    if not is_superadmin(
        message.from_user.id
    ):
        return

    rows = await list_promo_codes()

    if not rows:
        await message.answer(
            "Промокодов пока нет."
        )
        return

    lines = [
        "🎟 <b>Промокоды</b>",
        "",
    ]

    for row in rows:
        icon = (
            "🟢"
            if int(row["active"])
            else "🔴"
        )

        lines.append(
            f"{icon} "
            f"<code>{html.escape(row['code'])}</code>"
            f" — {row['discount_percent']}%"
            f" — использований: {row['uses']}"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )


@router.message(Command("promooff"))
async def cmd_promo_off(
    message: Message,
):
    if not is_superadmin(
        message.from_user.id
    ):
        return

    parts = (
        message.text or ""
    ).split()

    if len(parts) != 2:
        await message.answer(
            "Использование: "
            "<code>/promooff КОД</code>",
            parse_mode="HTML",
        )
        return

    ok = await disable_promo_code(
        parts[1]
    )

    if ok:
        await message.answer(
            "✅ Промокод отключён."
        )
    else:
        await message.answer(
            "❌ Такой промокод не найден."
        )



# =========================================================
# SUPERADMIN PROMOCODES UI
# =========================================================

@router.callback_query(
    F.data == "super:home"
)
async def cb_super_home_promos(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_superadmin(
        call.from_user.id
    ):
        return

    await state.clear()
    await call.answer()

    await call.message.answer(
        "⚙️ <b>Панель суперадмина</b>",
        parse_mode="HTML",
        reply_markup=super_keyboard(),
    )


@router.callback_query(
    F.data == "super:promos"
)
async def cb_super_promos(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_superadmin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    await state.clear()
    await call.answer()

    rows = await list_promo_codes()

    # В интерфейсе показываем только
    # действующие промокоды.
    active_rows = [
        row
        for row in rows
        if bool(int(row["active"]))
    ]

    lines = [
        "🎟 <b>Промокоды</b>",
        "",
    ]

    buttons = []

    if not active_rows:
        lines.append(
            "Активных промокодов пока нет."
        )

    else:
        for index, row in enumerate(
            active_rows[:40]
        ):
            code = str(
                row["code"]
            )

            percent = int(
                row["discount_percent"]
            )

            uses = int(
                row["uses"]
            )

            if percent == 100:
                price_text = "бесплатно"
            else:
                price = int(
                    1200
                    * (100 - percent)
                    / 100
                )

                price_text = (
                    f"{price} ₽"
                )

            lines.append(
                f"🟢 <code>"
                f"{html.escape(code)}"
                f"</code> — "
                f"<b>{percent}%</b>\n"
                f"Цена: {price_text}\n"
                f"Использований: {uses}"
            )

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"🗑 Удалить {code[:24]}"
                        ),
                        callback_data=(
                            "super:promo:pick:"
                            f"{index}"
                        ),
                    )
                ]
            )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    text="➕ Создать промокод",
                    callback_data=(
                        "super:promo:add"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=(
                        "super:promos"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=(
                        "admin:home"
                    ),
                )
            ],
        ]
    )

    await call.message.answer(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


@router.callback_query(
    F.data.startswith(
        "super:promo:pick:"
    )
)
async def cb_super_promo_delete_pick(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_superadmin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    try:
        index = int(
            call.data.rsplit(
                ":",
                1,
            )[1]
        )
    except Exception:
        await call.answer(
            "Некорректный промокод",
            show_alert=True,
        )
        return

    rows = await list_promo_codes()

    active_rows = [
        row
        for row in rows
        if bool(int(row["active"]))
    ]

    if (
        index < 0
        or index >= len(active_rows)
    ):
        await call.answer(
            "Список изменился. "
            "Откройте промокоды заново.",
            show_alert=True,
        )
        return

    row = active_rows[index]

    code = str(
        row["code"]
    )

    percent = int(
        row["discount_percent"]
    )

    await state.update_data(
        super_delete_promo_code=code
    )

    await call.answer()

    await call.message.answer(
        "⚠️ <b>Удалить промокод?</b>\n\n"
        f"Код: "
        f"<code>{html.escape(code)}</code>\n"
        f"Скидка: <b>{percent}%</b>\n\n"
        "После удаления новые пользователи "
        "не смогут его применить.\n"
        "История уже совершённых активаций "
        "сохранится.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Да, удалить",
                        callback_data=(
                            "super:promo:"
                            "confirm_delete"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=(
                            "super:promo:"
                            "cancel_delete"
                        ),
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    F.data == "super:promo:confirm_delete"
)
async def cb_super_promo_delete_confirm(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_superadmin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа",
            show_alert=True,
        )
        return

    data = await state.get_data()

    code = str(
        data.get(
            "super_delete_promo_code"
        )
        or ""
    ).strip()

    if not code:
        await state.clear()

        await call.answer(
            "Промокод не выбран",
            show_alert=True,
        )
        return

    ok = await disable_promo_code(
        code
    )

    await state.clear()

    if not ok:
        await call.answer(
            "Промокод уже удалён "
            "или не найден",
            show_alert=True,
        )
        return

    await call.answer(
        "Промокод удалён"
    )

    await call.message.edit_text(
        "✅ <b>Промокод удалён.</b>\n\n"
        f"<code>{html.escape(code)}</code>\n\n"
        "Он больше не действует.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎟 К промокодам",
                        callback_data=(
                            "super:promos"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ В админ-панель",
                        callback_data=(
                            "admin:home"
                        ),
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    F.data == "super:promo:cancel_delete"
)
async def cb_super_promo_delete_cancel(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_superadmin(
        call.from_user.id
    ):
        return

    await state.clear()

    await call.answer(
        "Удаление отменено"
    )

    await call.message.edit_text(
        "❌ Удаление промокода отменено.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎟 К промокодам",
                        callback_data=(
                            "super:promos"
                        ),
                    )
                ]
            ]
        ),
    )


@router.callback_query(
    F.data == "super:promo:add"
)
async def cb_super_promo_add(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_superadmin(
        call.from_user.id
    ):
        return

    await state.set_state(
        PaidStates.waiting_admin_promo_code
    )

    await call.answer()

    await call.message.answer(
        "🎟 <b>Создание промокода</b>\n\n"
        "Введите название промокода.\n\n"
        "Например:\n"
        "<code>WELCOME20</code>",
        parse_mode="HTML",
    )


@router.message(
    PaidStates.waiting_admin_promo_code
)
async def super_promo_code_received(
    message: Message,
    state: FSMContext,
):
    if not is_superadmin(
        message.from_user.id
    ):
        await state.clear()
        return

    code = (
        message.text or ""
    ).strip().upper()

    if len(code) < 2:
        await message.answer(
            "❌ Код слишком короткий. "
            "Введите другой."
        )
        return

    if len(code) > 40:
        await message.answer(
            "❌ Максимум 40 символов."
        )
        return

    await state.update_data(
        admin_promo_code=code
    )

    await state.set_state(
        PaidStates.waiting_admin_promo_discount
    )

    await message.answer(
        "Теперь укажите размер скидки "
        "от <b>1 до 100%</b>.\n\n"
        "Например:\n"
        "<code>20</code>\n\n"
        "Для бесплатной подписки:\n"
        "<code>100</code>",
        parse_mode="HTML",
    )


@router.message(
    PaidStates.waiting_admin_promo_discount
)
async def super_promo_discount_received(
    message: Message,
    state: FSMContext,
):
    if not is_superadmin(
        message.from_user.id
    ):
        await state.clear()
        return

    raw = (
        message.text or ""
    ).strip().replace("%", "")

    try:
        percent = int(raw)
    except ValueError:
        await message.answer(
            "❌ Введите число от 1 до 100."
        )
        return

    if not 1 <= percent <= 100:
        await message.answer(
            "❌ Скидка должна быть "
            "от 1 до 100%."
        )
        return

    data = await state.get_data()

    code = str(
        data.get(
            "admin_promo_code"
        )
        or ""
    ).strip()

    if not code:
        await state.clear()

        await message.answer(
            "❌ Код промокода потерян. "
            "Создайте его заново.",
            reply_markup=(
                super_promos_keyboard()
            ),
        )
        return

    try:
        promo = await create_promo_code(
            code,
            percent,
        )
    except Exception as exc:
        log.exception(
            "Ошибка создания promo "
            "из superadmin UI"
        )

        await message.answer(
            "❌ Не удалось создать "
            "промокод:\n"
            f"<code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    await state.clear()

    if percent == 100:
        price_text = (
            "0 ₽ — подписка активируется "
            "сразу после ввода промокода"
        )
    else:
        final_kopecks = int(
            promo["final_kopecks"]
        )

        final_text = (
            f"{final_kopecks / 100:,.2f}"
            .replace(",", " ")
            .replace(".00", "")
        )

        price_text = (
            f"{final_text} ₽"
        )

    await message.answer(
        "✅ <b>Промокод создан</b>\n\n"
        f"Код: "
        f"<code>{html.escape(promo['code'])}</code>\n"
        f"Скидка: "
        f"<b>{promo['discount_percent']}%</b>\n"
        f"Цена: <b>{price_text}</b>\n\n"
        "Один пользователь может "
        "использовать промокод один раз.",
        parse_mode="HTML",
        reply_markup=(
            super_promos_keyboard()
        ),
    )
