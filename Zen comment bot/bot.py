from __future__ import annotations

import asyncio
import html
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from comment_worker import CommentWorker
from config import Settings
from database import Database
from dzen_browser import DzenBrowser
from yandexgpt import YandexGPTClient

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("dzen-comment-bot")

router = Router()
settings = Settings.from_env()
db = Database(settings.db_path)
browser = DzenBrowser(settings)
ai = YandexGPTClient(settings)
worker = CommentWorker(settings, db, browser, ai)

KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="▶️ Включить"), KeyboardButton(text="⏸ Выключить")],
        [KeyboardButton(text="🔎 Проверить сейчас"), KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="💬 Последние ответы"), KeyboardButton(text="🔐 Авторизация Дзена")],
    ],
    resize_keyboard=True,
)


def admin_only(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in settings.telegram_admin_ids
    )


async def deny(message: Message) -> None:
    await message.answer("У Вас нет доступа к управлению этим ботом.")


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return

    errors = settings.validate_runtime()

    if errors:
        body = (
            "Бот установлен, но требуется настройка:\n• "
            + "\n• ".join(map(html.escape, errors))
        )
    else:
        if settings.dry_run:
            body = (
                "✅ Dzen AI Comment Bot готов.\n\n"
                "🧪 Режим: DRY_RUN\n"
                "Ответы генерируются, но в Дзен не публикуются."
            )
        else:
            body = (
                "✅ Dzen AI Comment Bot готов.\n\n"
                "🚀 Режим: БОЕВОЙ\n"
                "Автоматическая публикация ответов в Дзен разрешена."
            )

    await message.answer(body, reply_markup=KB)


@router.message(Command("on"))
@router.message(F.text == "▶️ Включить")
async def turn_on(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return
    await worker.set_enabled(True)
    await message.answer("✅ Автоответы включены. Бот будет проверять новые комментарии по расписанию.")


@router.message(Command("off"))
@router.message(F.text == "⏸ Выключить")
async def turn_off(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return
    await worker.set_enabled(False)
    await message.answer("⏸ Автоответы выключены.")


@router.message(Command("check"))
@router.message(F.text == "🔎 Проверить сейчас")
async def check_now(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return
    await message.answer("Проверяю новые комментарии…")
    try:
        results = await worker.run_once()
    except Exception as exc:
        await message.answer(f"❌ Проверка не выполнена: <code>{html.escape(str(exc))}</code>")
        return
    if not results:
        await message.answer("Новых необработанных комментариев не найдено.")
        return
    replied = sum(1 for r in results if r.action == "reply")
    review = sum(1 for r in results if r.action == "review")
    skipped = sum(1 for r in results if r.action == "skip")
    mode = "DRY RUN" if settings.dry_run else "БОЕВОЙ"
    await message.answer(f"Готово. Режим: <b>{mode}</b>\nОтветов: {replied}\nНа проверку: {review}\nПропущено: {skipped}")


@router.message(Command("status"))
@router.message(F.text == "📊 Статус")
async def status(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return
    enabled = await worker.enabled()
    stats = await db.stats()
    auth_ok, auth_info = await browser.check_auth()
    await message.answer(
        "<b>Dzen AI Comment Bot v1</b>\n"
        f"Автоответы: {'✅ включены' if enabled else '⏸ выключены'}\n"
        f"Режим: {'🧪 DRY_RUN' if settings.dry_run else '🚀 публикация'}\n"
        f"Дзен: {'✅ авторизован' if auth_ok else '❌ нужна авторизация'}\n"
        f"Интервал: {settings.poll_seconds} сек.\n"
        f"Обработано: {stats['total']}\n"
        f"Опубликовано: {stats['replied']}\n"
        f"Пропущено: {stats['skipped']}\n"
        f"На проверке: {stats['review']}\n"
        f"Последняя ошибка: {html.escape(worker.last_error or 'нет')}\n"
        f"URL сессии: <code>{html.escape(auth_info[:300])}</code>"
    )


@router.message(Command("last"))
@router.message(F.text == "💬 Последние ответы")
async def last(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return
    rows = await db.recent(5)
    if not rows:
        await message.answer("История пока пустая.")
        return
    parts: list[str] = []
    for row in rows:
        status_emoji = "✅" if row["published"] else ("⏭" if row["action"] == "skip" else "🟡")
        comment = html.escape((row["comment_text"] or "")[:350])
        reply = html.escape((row["final_reply"] or row["ai_reply"] or row["reason"] or "")[:650])
        parts.append(f"{status_emoji} <b>{html.escape(row['author'] or 'Комментарий')}</b>\n{comment}\n\n{reply}")
    await message.answer("\n\n──────────\n\n".join(parts))


@router.message(Command("auth"))
@router.message(F.text == "🔐 Авторизация Дзена")
async def auth_status(message: Message) -> None:
    if not admin_only(message):
        await deny(message)
        return
    ok, info = await browser.check_auth()
    if ok:
        await message.answer(f"✅ Сессия Дзена работает.\n<code>{html.escape(info[:500])}</code>")
    else:
        await message.answer(
            "❌ Авторизация Дзена не подтверждена. Запустите <code>python setup_dzen.py</code> на компьютере с экраном, войдите в нужный аккаунт и перенесите полученный <code>data/dzen_state.json</code> на сервер.\n\n"
            f"Диагностика: <code>{html.escape(info[:500])}</code>"
        )


async def main() -> None:
    await db.init()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.debug_dir.mkdir(parents=True, exist_ok=True)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    worker_task = asyncio.create_task(worker.loop(), name="comment-worker")
    try:
        await dp.start_polling(bot)
    finally:
        await worker.stop()
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        await browser.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
