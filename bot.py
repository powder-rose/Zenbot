from __future__ import annotations

import asyncio
import html
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
import tenant_db
from article_service import (
    ArticleService,
    DEFAULT_IMAGE_PROMPT_TEMPLATE,
)
from config import load_config
from image_gen import YandexArtClient
from keyboards import (
    admin_menu,
    schedule_delete,
    schedule_menu,
    topic_actions,
    topics_menu,
    urgent_menu,
    prompts_menu,
    prompt_detail_menu,
    prompt_edit_menu,
)
from scheduler import AutoPublisher
from tenant_scheduler import TenantScheduler
from tenant_service import TenantArticleService
from dzen_popular_comments import DzenPopularCommentWorker
from paid_multiuse import (
    router as paid_router,
    configure_paid_multiuse,
    show_paid_start,
    show_cabinet as show_paid_cabinet,
)
from search import YandexSearchClient
from states import AdminStates
from topics_seed import DEFAULT_TOPICS
from yandex_gpt import (
    ARTICLE_SYSTEM_PROMPT,
    SYNCBOT_SYSTEM_PROMPT,
    YandexGPTClient,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("zen-bot")

dp = Dispatcher(storage=MemoryStorage())
dp.include_router(paid_router)
cfg = load_config()

gpt_client = YandexGPTClient(
    folder_id=cfg.yc_folder_id,
    api_key=cfg.yc_api_key,
    sa_key_file=cfg.yc_sa_key_file,
)
search_client = YandexSearchClient(
    folder_id=cfg.yc_folder_id,
    get_auth_header=gpt_client.auth_header,
)
art_client = YandexArtClient(
    folder_id=cfg.yc_folder_id,
    get_auth_header=gpt_client.auth_header,
)

service: ArticleService | None = None
scheduler: AutoPublisher | None = None
tenant_service: TenantArticleService | None = None
tenant_scheduler: TenantScheduler | None = None
popular_comment_worker: DzenPopularCommentWorker | None = None

def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in cfg.admin_ids)

async def deny(message_or_call) -> bool:
    user_id = getattr(getattr(message_or_call, "from_user", None), "id", None)
    if is_admin(user_id):
        return False
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.answer("Нет доступа", show_alert=True)
    else:
        await message_or_call.answer("⛔ Нет доступа.")
    return True

async def show_admin(message: Message) -> None:
    auto = await db.auto_publish_enabled()
    await message.answer(
        "⚙️ <b>Управление автопубликацией</b>\n\n"
        f"Автопубликация: {'🟢 включена' if auto else '🔴 выключена'}\n"
        f"Telegram: <code>{html.escape(str(cfg.telegram_channel_id))}</code>\n",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id):
        await show_admin(message)
    else:
        await show_paid_start(message, state)

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id):
        await show_admin(message)
        return
    await show_paid_cabinet(message)

@dp.callback_query(F.data == "admin:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    if await deny(call):
        return
    await state.clear()
    await call.answer()
    await show_admin(call.message)


@dp.callback_query(F.data == "admin:restart")
async def cb_restart(call: CallbackQuery, state: FSMContext):
    if await deny(call):
        return

    await state.clear()
    await call.answer("Перезапускаю…")
    await call.message.answer("🔄 Перезапускаю бота…")
    await asyncio.sleep(1)

    os.execv(
        sys.executable,
        [sys.executable, *sys.argv],
    )

# ---------------- ТЕМЫ

@dp.callback_query(F.data == "admin:topics")
async def cb_topics(call: CallbackQuery):
    if await deny(call):
        return
    await call.answer()
    await call.message.answer(
        "📝 <b>Темы</b>\n\n"
        "Автоматически берётся только тема, которая ещё ни разу не использовалась.",
        parse_mode="HTML",
        reply_markup=topics_menu(),
    )

@dp.callback_query(F.data == "topics:add")
async def cb_topics_add(call: CallbackQuery, state: FSMContext):
    if await deny(call):
        return
    await state.set_state(AdminStates.waiting_topics)
    await call.answer()
    await call.message.answer(
        "Пришлите темы одним сообщением.\nКаждая новая строка = отдельная тема."
    )

@dp.message(AdminStates.waiting_topics)
async def process_topics_add(message: Message, state: FSMContext):
    if await deny(message):
        return
    lines = (message.text or "").splitlines()
    count = await db.add_topics(lines)
    await state.clear()
    await message.answer(f"✅ Добавлено новых тем: {count}", reply_markup=topics_menu())

async def send_topics_list(message: Message, mode: str) -> None:
    rows = await db.list_topics(mode=mode, limit=50)
    title = "Неиспользованные темы" if mode == "unused" else "Использованные темы"
    if not rows:
        await message.answer(f"📋 {title}: список пуст.")
        return

    await message.answer(f"📋 <b>{title}</b>: {len(rows)}", parse_mode="HTML")
    for row in rows:
        used = f"\nОпубликована: {row['used_at']}" if row["used_at"] else ""
        await message.answer(
            f"#{row['id']} — {html.escape(row['title'])}{used}",
            parse_mode="HTML",
            reply_markup=topic_actions(int(row["id"])),
        )

@dp.callback_query(F.data.startswith("topics:list:"))
async def cb_topics_list(call: CallbackQuery):
    if await deny(call):
        return
    mode = call.data.rsplit(":", 1)[1]
    await call.answer()
    await send_topics_list(call.message, mode)

@dp.callback_query(F.data.startswith("topic:edit:"))
async def cb_topic_edit(call: CallbackQuery, state: FSMContext):
    if await deny(call):
        return
    topic_id = int(call.data.rsplit(":", 1)[1])
    row = await db.get_topic(topic_id)
    if not row:
        await call.answer("Тема не найдена", show_alert=True)
        return
    await state.update_data(topic_id=topic_id)
    await state.set_state(AdminStates.waiting_edit_topic)
    await call.answer()
    await call.message.answer(
        f"Текущее название:\n{row['title']}\n\nПришлите новое название:"
    )

@dp.message(AdminStates.waiting_edit_topic)
async def process_topic_edit(message: Message, state: FSMContext):
    if await deny(message):
        return
    data = await state.get_data()
    ok = await db.update_topic(int(data["topic_id"]), message.text or "")
    await state.clear()
    if ok:
        await message.answer("✅ Тема изменена.", reply_markup=topics_menu())
    else:
        await message.answer(
            "❌ Не удалось изменить тему. Возможно, такое название уже существует.",
            reply_markup=topics_menu(),
        )

@dp.callback_query(F.data.startswith("topic:delete:"))
async def cb_topic_delete(call: CallbackQuery):
    if await deny(call):
        return
    topic_id = int(call.data.rsplit(":", 1)[1])
    ok = await db.deactivate_topic(topic_id)
    await call.answer("Удалено" if ok else "Тема не найдена")
    if ok:
        await call.message.edit_text("🗑 Тема удалена из активного списка.")

# ---------------- РАСПИСАНИЕ

async def send_schedule(message: Message) -> None:
    rows = await db.list_schedule()
    auto = await db.auto_publish_enabled()
    await message.answer(
        "⏰ <b>Расписание</b>\n\n"
        f"Статус: {'🟢 включено' if auto else '🔴 выключено'}\n"
        f"Часовой пояс: <code>{html.escape(cfg.timezone)}</code>",
        parse_mode="HTML",
        reply_markup=schedule_menu(),
    )
    if not rows:
        await message.answer("Время публикаций пока не задано.")
        return
    for row in rows:
        await message.answer(
            f"🕒 {row['publish_time']}",
            reply_markup=schedule_delete(int(row["id"])),
        )

@dp.callback_query(F.data == "admin:schedule")
async def cb_schedule(call: CallbackQuery):
    if await deny(call):
        return
    await call.answer()
    await send_schedule(call.message)

@dp.callback_query(F.data == "schedule:add")
async def cb_schedule_add(call: CallbackQuery, state: FSMContext):
    if await deny(call):
        return
    await state.set_state(AdminStates.waiting_schedule_time)
    await call.answer()
    await call.message.answer("Введите время публикации в формате ЧЧ:ММ.\nНапример: 09:00")

@dp.message(AdminStates.waiting_schedule_time)
async def process_schedule_add(message: Message, state: FSMContext):
    if await deny(message):
        return
    value = (message.text or "").strip()
    ok = await db.add_schedule_time(value)
    if not ok:
        await message.answer("❌ Неверный формат или такое время уже есть.\nВведите, например: 13:30")
        return
    await state.clear()
    await message.answer("✅ Время добавлено.")
    await send_schedule(message)

@dp.callback_query(F.data.startswith("schedule:delete:"))
async def cb_schedule_delete(call: CallbackQuery):
    if await deny(call):
        return
    schedule_id = int(call.data.rsplit(":", 1)[1])
    await db.delete_schedule_time(schedule_id)
    await call.answer("Удалено")
    await call.message.edit_text("🗑 Время удалено.")

@dp.callback_query(F.data == "schedule:toggle")
async def cb_schedule_toggle(call: CallbackQuery):
    if await deny(call):
        return
    current = await db.auto_publish_enabled()
    await db.set_auto_publish_enabled(not current)
    await call.answer("Настройка изменена")
    await send_schedule(call.message)


# ---------------- ПРОМПТЫ

PROMPT_SETTINGS = {
    "article": {
        "title": "✍️ Основной текст статьи",
        "key": "prompt_article_system",
        "default": ARTICLE_SYSTEM_PROMPT,
        "filename": "article_prompt.txt",
    },
    "short": {
        "title": "📱 Короткая версия Telegram",
        "key": "prompt_short_system",
        "default": SYNCBOT_SYSTEM_PROMPT,
        "filename": "short_prompt.txt",
    },
    "image": {
        "title": "🖼 Промпт изображения",
        "key": "prompt_image_template",
        "default": DEFAULT_IMAGE_PROMPT_TEMPLATE,
        "filename": "image_prompt.txt",
    },
}


async def effective_prompt(kind: str) -> tuple[str, bool]:
    meta = PROMPT_SETTINGS[kind]
    custom = (
        await db.get_setting(
            meta["key"],
            "",
        )
    ).strip()

    if custom:
        return custom, True

    return meta["default"], False


async def send_prompt_card(
    message: Message,
    kind: str,
) -> None:
    meta = PROMPT_SETTINGS[kind]
    prompt, is_custom = await effective_prompt(
        kind
    )

    status = (
        "🟢 пользовательский"
        if is_custom
        else "⚪ стандартный"
    )

    await message.answer(
        f"{meta['title']}\n\n"
        f"Сейчас используется: {status}\n"
        f"Длина: {len(prompt)} символов.\n\n"
        "Текущий промпт отправлен TXT-файлом ниже.",
        reply_markup=prompt_detail_menu(
            kind
        ),
    )

    await message.answer_document(
        BufferedInputFile(
            prompt.encode("utf-8"),
            filename=meta["filename"],
        ),
        caption=(
            "Текущий активный промпт. "
            "TXT удобнее, потому что длинный текст "
            "может превышать лимит одного сообщения Telegram."
        ),
    )


@dp.callback_query(F.data == "admin:prompts")
async def cb_prompts(
    call: CallbackQuery,
    state: FSMContext,
):
    if await deny(call):
        return

    await state.clear()
    await call.answer()

    await call.message.answer(
        "🧠 <b>Промпты</b>\n\n"
        "Изменения сохраняются в базе и начинают действовать "
        "со следующей создаваемой статьи.\n\n"
        "Для изображения можно использовать маркер "
        "<code>{topic}</code> — бот подставит текущую тему.",
        parse_mode="HTML",
        reply_markup=prompts_menu(),
    )


@dp.callback_query(F.data.startswith("prompt:view:"))
async def cb_prompt_view(
    call: CallbackQuery,
    state: FSMContext,
):
    if await deny(call):
        return

    kind = call.data.rsplit(
        ":",
        1,
    )[1]

    if kind not in PROMPT_SETTINGS:
        await call.answer(
            "Неизвестный промпт",
            show_alert=True,
        )
        return

    await state.clear()
    await call.answer()
    await send_prompt_card(
        call.message,
        kind,
    )


@dp.callback_query(F.data.startswith("prompt:edit:"))
async def cb_prompt_edit(
    call: CallbackQuery,
    state: FSMContext,
):
    if await deny(call):
        return

    kind = call.data.rsplit(
        ":",
        1,
    )[1]

    if kind not in PROMPT_SETTINGS:
        await call.answer(
            "Неизвестный промпт",
            show_alert=True,
        )
        return

    await state.clear()
    await state.update_data(
        prompt_kind=kind,
        prompt_parts=[],
    )
    await state.set_state(
        AdminStates.waiting_prompt_parts
    )

    await call.answer()

    extra = ""
    if kind == "image":
        extra = (
            "\n\nДля изображения желательно оставить "
            "маркер {topic}. Финальный запрос YandexART "
            "будет ограничен 480 символами."
        )

    await call.message.answer(
        f"{PROMPT_SETTINGS[kind]['title']}\n\n"
        "Отправь новый промпт текстом.\n"
        "Если он длиннее лимита Telegram — отправляй "
        "несколькими сообщениями подряд.\n\n"
        "Я буду складывать все сообщения в один промпт. "
        "Когда закончишь — нажми «✅ Сохранить»."
        f"{extra}",
        reply_markup=prompt_edit_menu(),
    )


@dp.message(AdminStates.waiting_prompt_parts)
async def process_prompt_part(
    message: Message,
    state: FSMContext,
):
    if await deny(message):
        return

    part = message.text or ""

    if not part.strip():
        await message.answer(
            "Пришли текст промпта. "
            "Затем нажми «✅ Сохранить».",
            reply_markup=prompt_edit_menu(),
        )
        return

    data = await state.get_data()
    parts = list(
        data.get(
            "prompt_parts",
            [],
        )
    )
    parts.append(
        part
    )

    total = sum(
        len(value)
        for value in parts
    ) + max(
        0,
        len(parts) - 1
    ) * 2

    await state.update_data(
        prompt_parts=parts
    )

    await message.answer(
        f"✅ Фрагмент добавлен.\n"
        f"Фрагментов: {len(parts)}\n"
        f"Общая длина: {total} символов.",
        reply_markup=prompt_edit_menu(),
    )


@dp.callback_query(F.data == "prompt:save")
async def cb_prompt_save(
    call: CallbackQuery,
    state: FSMContext,
):
    if await deny(call):
        return

    data = await state.get_data()
    kind = data.get(
        "prompt_kind"
    )
    parts = data.get(
        "prompt_parts",
        [],
    )

    if kind not in PROMPT_SETTINGS:
        await call.answer(
            "Редактор не активен",
            show_alert=True,
        )
        return

    prompt = "\n\n".join(
        str(part).strip()
        for part in parts
        if str(part).strip()
    ).strip()

    if not prompt:
        await call.answer(
            "Сначала отправь текст промпта",
            show_alert=True,
        )
        return

    await db.set_setting(
        PROMPT_SETTINGS[kind]["key"],
        prompt,
    )

    await state.clear()
    await call.answer(
        "Промпт сохранён"
    )

    await call.message.answer(
        f"✅ {PROMPT_SETTINGS[kind]['title']} обновлён.\n"
        f"Длина: {len(prompt)} символов.\n\n"
        "Новый промпт будет применён к следующей статье.",
        reply_markup=prompts_menu(),
    )


@dp.callback_query(F.data.startswith("prompt:reset:"))
async def cb_prompt_reset(
    call: CallbackQuery,
    state: FSMContext,
):
    if await deny(call):
        return

    kind = call.data.rsplit(
        ":",
        1,
    )[1]

    if kind not in PROMPT_SETTINGS:
        await call.answer(
            "Неизвестный промпт",
            show_alert=True,
        )
        return

    await db.set_setting(
        PROMPT_SETTINGS[kind]["key"],
        "",
    )
    await state.clear()
    await call.answer(
        "Сброшено"
    )

    await call.message.answer(
        f"♻️ {PROMPT_SETTINGS[kind]['title']} "
        "сброшен к стандартному.",
        reply_markup=prompts_menu(),
    )


@dp.callback_query(F.data == "prompt:cancel")
async def cb_prompt_cancel(
    call: CallbackQuery,
    state: FSMContext,
):
    if await deny(call):
        return

    await state.clear()
    await call.answer(
        "Изменения отменены"
    )

    await call.message.answer(
        "❌ Черновик промпта удалён. "
        "Сохранённый промпт не менялся.",
        reply_markup=prompts_menu(),
    )


# ---------------- СРОЧНАЯ СТАТЬЯ

@dp.callback_query(F.data == "admin:urgent")
async def cb_urgent(call: CallbackQuery):
    if await deny(call):
        return
    await call.answer()
    await call.message.answer(
        "⚡ <b>Срочная статья</b>\nПубликация запускается сразу и не ждёт расписания.",
        parse_mode="HTML",
        reply_markup=urgent_menu(),
    )

def result_text(result: dict) -> str:
    status = result.get("status")
    if status == "no_topics":
        return "⚠️ Нет активных тем для публикации."
    if status != "ok":
        return "❌ Статья не создана.\nОшибка: " + str(result.get("error", "неизвестная ошибка"))
    return (
        "✅ Статья обработана.\n\n"
        f"Тема: {result.get('topic')}\n"
        f"Заголовок: {result.get('article_title')}\n"
        f"Telegram: {result.get('telegram')}"
    )

@dp.callback_query(F.data == "urgent:random")
async def cb_urgent_random(call: CallbackQuery):
    if await deny(call):
        return
    if service is None:
        await call.answer("Сервис ещё не запущен", show_alert=True)
        return
    await call.answer()
    status = await call.message.answer(
        "⚡ Выбираю случайную неиспользованную тему и начинаю публикацию…"
    )
    result = await service.publish_random_topic(trigger="urgent_random")
    await status.edit_text(result_text(result))

@dp.callback_query(F.data == "urgent:manual")
async def cb_urgent_manual(call: CallbackQuery, state: FSMContext):
    if await deny(call):
        return
    await state.set_state(AdminStates.waiting_urgent_topic)
    await call.answer()
    await call.message.answer("Введите тему срочной статьи:")

@dp.message(AdminStates.waiting_urgent_topic)
async def process_urgent_manual(message: Message, state: FSMContext):
    if await deny(message):
        return
    if service is None:
        await message.answer("Сервис ещё не запущен.")
        return
    topic = " ".join((message.text or "").split())
    await state.clear()
    status = await message.answer(f"⚡ Создаю внеплановую статью по теме:\n{topic}")
    result = await service.publish_manual_topic(topic)
    await status.edit_text(result_text(result))

# ---------------- ПОПУЛЯРНЫЙ КОММЕНТАРИЙ ДЗЕН ----------------

def dzen_comment_control_keyboard() -> InlineKeyboardMarkup:
    if popular_comment_worker is None:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]]
        )
    status = popular_comment_worker.status()
    auto_on = bool(status.get("enabled"))
    preview_on = bool(status.get("preview_enabled"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("🟢 Авто по комментариям: ВКЛ" if auto_on else "🔴 Авто по комментариям: ВЫКЛ"),
                    callback_data="dzenpc:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("👁 Предпросмотр: ВКЛ" if preview_on else "⚡ Предпросмотр: ВЫКЛ"),
                    callback_data="dzenpc:previewtoggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔥 Создать предпросмотр сейчас",
                    callback_data="dzenpc:previewnow",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
        ]
    )


def dzen_comment_control_text() -> str:
    if popular_comment_worker is None:
        return "🔥 <b>Комментарии Дзена</b>\n\nСервис ещё не запущен."
    status = popular_comment_worker.status()
    auto_on = bool(status.get("enabled"))
    preview_on = bool(status.get("preview_enabled"))
    pending = int(status.get("pending_count") or 0)
    interval_min = max(1, int(status.get("interval_seconds") or 0) // 60)
    return (
        "🔥 <b>Статьи по популярным комментариям Дзена</b>\n\n"
        f"Автоматический поиск: {'🟢 ВКЛ' if auto_on else '🔴 ВЫКЛ'}\n"
        f"Предпросмотр перед публикацией: {'🟢 ВКЛ' if preview_on else '🔴 ВЫКЛ'}\n"
        f"Минимум лайков: <b>{status.get('min_likes', 0)}</b>\n"
        f"Проверка: каждые <b>{interval_min} мин.</b>\n"
        f"Ожидают решения: <b>{pending}</b>\n\n"
        "Если предпросмотр включён, бот найдёт новый самый залайканный комментарий, "
        "полностью сгенерирует LONG, SHORT и картинку и пришлёт их сюда. "
        "В канал статья уйдёт только после кнопки «✅ Опубликовать».\n\n"
        "Если предпросмотр выключен, новый лидер публикуется автоматически."
    )


async def show_dzen_comment_control(message: Message, *, edit: bool = False) -> None:
    text = dzen_comment_control_text()
    kb = dzen_comment_control_keyboard()
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def handle_preview_creation(target: Message, *, chat_id: int) -> None:
    if popular_comment_worker is None:
        await target.answer("Сервис популярных комментариев ещё не запущен.")
        return
    status_message = await target.answer(
        "🔥 Ищу самый залайканный комментарий и создаю полный предпросмотр статьи…"
    )
    try:
        result = await popular_comment_worker.create_preview(force=True)
    except Exception as exc:
        log.exception("Предпросмотр Dzen-комментария завершился ошибкой")
        await status_message.edit_text(f"❌ Ошибка создания предпросмотра: {exc}")
        return

    state = str(result.get("status") or "")
    if state == "preview_ready":
        await status_message.edit_text(
            "✅ Предпросмотр готов. Ниже отправляю комментарий, картинку, LONG и SHORT.\n"
            "Публикации пока не было."
        )
        await popular_comment_worker.send_preview_to_admins(result, chat_ids=[chat_id])
    elif state == "top_rejected":
        await status_message.edit_text(
            "ℹ️ Предпросмотр по текущему лидеру ранее был отменён. "
            "Бот ждёт, пока лидером станет другой комментарий.\n\n"
            f"❤️ Лайков: {result.get('likes', 0)}"
        )
    elif state == "preview_pending":
        preview_id = str(result.get("preview_id") or "")
        existing = popular_comment_worker.get_preview(preview_id)
        if existing:
            existing["status"] = "preview_ready"
            await status_message.edit_text("ℹ️ Для текущего лидера предпросмотр уже создан. Показываю его ещё раз.")
            await popular_comment_worker.send_preview_to_admins(existing, chat_ids=[chat_id])
        else:
            await status_message.edit_text("Предпросмотр отмечен как ожидающий, но его данные не найдены.")
    elif state == "no_comments":
        await status_message.edit_text("Комментарии в Дзене не найдены.")
    elif state == "no_liked_comments":
        await status_message.edit_text(
            "Нет комментариев, достигших минимального количества лайков. "
            f"Максимум сейчас: {result.get('max_likes', 0)}."
        )
    elif state == "no_fresh_comments":
        await status_message.edit_text(
            "Новых неиспользованных комментариев для статьи пока нет."
        )
    elif state == "no_article_worthy_comments":
        await status_message.edit_text("Популярные комментарии найдены, но подходящей темы для статьи нет.")
    else:
        await status_message.edit_text(
            "Не удалось создать предпросмотр.\n\n"
            f"Статус: {state}\n"
            f"Ошибка: {result.get('error') or '—'}"
        )


@dp.message(Command("dzencomment"))
async def cmd_dzen_popular_comment(message: Message):
    if await deny(message):
        return
    await show_dzen_comment_control(message)


@dp.callback_query(F.data == "admin:dzencomments")
async def cb_dzen_comments_panel(call: CallbackQuery):
    if await deny(call):
        return
    await call.answer()
    await show_dzen_comment_control(call.message)


@dp.callback_query(F.data == "dzenpc:toggle")
async def cb_dzen_comments_toggle(call: CallbackQuery):
    if await deny(call):
        return
    if popular_comment_worker is None:
        await call.answer("Сервис ещё не запущен", show_alert=True)
        return
    enabled = popular_comment_worker.toggle_enabled()
    await call.answer("Авто включено" if enabled else "Авто выключено")
    await show_dzen_comment_control(call.message, edit=True)


@dp.callback_query(F.data == "dzenpc:previewtoggle")
async def cb_dzen_preview_toggle(call: CallbackQuery):
    if await deny(call):
        return
    if popular_comment_worker is None:
        await call.answer("Сервис ещё не запущен", show_alert=True)
        return
    enabled = popular_comment_worker.toggle_preview_enabled()
    await call.answer("Предпросмотр включён" if enabled else "Предпросмотр выключен")
    await show_dzen_comment_control(call.message, edit=True)


@dp.callback_query(F.data == "dzenpc:previewnow")
async def cb_dzen_preview_now(call: CallbackQuery):
    if await deny(call):
        return
    await call.answer("Создаю предпросмотр")
    await handle_preview_creation(call.message, chat_id=call.from_user.id)


@dp.callback_query(F.data.startswith("dzenpc:publish:"))
async def cb_dzen_preview_publish(call: CallbackQuery):
    if await deny(call):
        return
    if popular_comment_worker is None:
        await call.answer("Сервис ещё не запущен", show_alert=True)
        return
    preview_id = call.data.rsplit(":", 1)[1]
    await call.answer("Публикую")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    status_message = await call.message.answer(
        "🚀 Публикую именно эту согласованную версию. Повторной генерации не будет…"
    )
    try:
        result = await popular_comment_worker.publish_preview(preview_id)
    except Exception as exc:
        log.exception("Ошибка публикации согласованного Dzen preview")
        await status_message.edit_text(f"❌ Ошибка публикации: {exc}")
        return

    if result.get("status") == "ok":
        await status_message.edit_text(
            "✅ Согласованная статья опубликована LONG-постом. "
            "Дальше сработает обычный цикл Дзен → удаление LONG → SHORT в Telegram."
        )
    elif result.get("status") == "preview_not_found":
        await status_message.edit_text("❌ Этот предпросмотр уже опубликован, отменён или больше не существует.")
    else:
        error = (result.get("publish_result") or {}).get("error") or result.get("error") or result.get("status")
        await status_message.edit_text(f"❌ Не удалось опубликовать предпросмотр: {error}")


@dp.callback_query(F.data.startswith("dzenpc:cancel:"))
async def cb_dzen_preview_cancel(call: CallbackQuery):
    if await deny(call):
        return
    if popular_comment_worker is None:
        await call.answer("Сервис ещё не запущен", show_alert=True)
        return
    preview_id = call.data.rsplit(":", 1)[1]
    ok = popular_comment_worker.cancel_preview(preview_id)
    await call.answer("Предпросмотр отменён" if ok else "Предпросмотр не найден")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if ok:
        await call.message.answer(
            "❌ Предпросмотр отменён. По этому комментарию статья не будет создана повторно, "
            "пока лидером не станет другой комментарий."
        )


# ---------------- СТАТИСТИКА

@dp.callback_query(F.data == "admin:stats")
async def cb_stats(call: CallbackQuery):
    if await deny(call):
        return
    await call.answer()
    stats = await db.stats()
    await call.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Всего статей: {stats['total']}\n"
        f"Активных тем: {stats['active']}\n"
        f"Ещё ни разу не использовались: {stats['unused']}\n"
        f"Уже использовались: {stats['used']}\n"
        f"Telegram опубликовано: {stats['telegram_published']}\n"
        f"Последняя статья: {html.escape(stats['last_title'] or '—')}",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )

async def main():
    global service, scheduler, tenant_service, tenant_scheduler, popular_comment_worker

    await db.init_db(cfg.db_path)
    await tenant_db.init_db(cfg.db_path)
    seeded = await db.seed_topics(DEFAULT_TOPICS)
    log.info("Добавлено стартовых тем: %s", seeded)

    await db.ensure_default_daily_schedule(
        cfg.default_publish_times
    )

    bot = Bot(token=cfg.bot_token)

    service = ArticleService(
        bot=bot,
        cfg=cfg,
        gpt_client=gpt_client,
        search_client=search_client,
        art_client=art_client,
    )
    scheduler = AutoPublisher(service, cfg)

    popular_comment_worker = DzenPopularCommentWorker(
        article_service=service,
        gpt_client=gpt_client,
        cfg=cfg,
    )

    tenant_service = TenantArticleService(
        bot=bot,
        cfg=cfg,
        gpt_client=gpt_client,
        search_client=search_client,
        art_client=art_client,
    )
    tenant_scheduler = TenantScheduler(tenant_service, cfg)
    configure_paid_multiuse(
        bot=bot,
        cfg=cfg,
        service=tenant_service,
    )

    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть бота"),
        BotCommand(command="admin", description="Панель администратора"),
        BotCommand(command="cabinet", description="Мой кабинет"),
        BotCommand(command="buy", description="Оплатить подписку"),
        BotCommand(command="subscription", description="Моя подписка"),
        BotCommand(command="dzencomment", description="Управление статьями по комментариям Дзена"),
    ])

    scheduler_task = asyncio.create_task(scheduler.run())
    tenant_scheduler_task = asyncio.create_task(tenant_scheduler.run())
    popular_comment_task = asyncio.create_task(popular_comment_worker.run())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        tenant_scheduler.stop()
        popular_comment_worker.stop()
        scheduler_task.cancel()
        tenant_scheduler_task.cancel()
        popular_comment_task.cancel()
        for task in (scheduler_task, tenant_scheduler_task, popular_comment_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

        if service is not None:
            try:
                await service.close()
            except Exception:
                log.exception("Ошибка закрытия Telegram Web publisher")

        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
