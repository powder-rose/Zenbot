from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from playwright.async_api import async_playwright

import tenant_db


log = logging.getLogger("tenant_dzen_qr_auth")

_AUTH_LOCKS: dict[int, asyncio.Lock] = {}


def _lock(user_id: int) -> asyncio.Lock:
    lock = _AUTH_LOCKS.get(user_id)

    if lock is None:
        lock = asyncio.Lock()
        _AUTH_LOCKS[user_id] = lock

    return lock


async def _first_visible(locators):
    for locator in locators:
        try:
            count = await locator.count()
        except Exception:
            continue

        for index in range(min(count, 10)):
            item = locator.nth(index)

            try:
                if await item.is_visible():
                    return item
            except Exception:
                continue

    return None


async def _open_qr_login(
    page,
    yandex_login: str,
) -> None:
    await page.goto(
        "https://passport.yandex.ru/auth",
        wait_until="domcontentloaded",
        timeout=90000,
    )

    await page.wait_for_timeout(1500)

    # Если браузер уже авторизован,
    # форму логина вообще не трогаем.
    if "passport.yandex.ru" not in page.url:
        return

    login_input = await _first_visible(
        [
            page.locator(
                'input[name="login"]'
            ),
            page.locator(
                'input[data-t="field:input-login"]'
            ),
            page.locator(
                'input[type="email"]'
            ),
            page.locator(
                'input[type="text"]'
            ),
        ]
    )

    if login_input is not None:
        await login_input.fill(
            yandex_login
        )

        submit = await _first_visible(
            [
                page.locator(
                    'button[type="submit"]'
                ),
                page.get_by_text(
                    re.compile(
                        r"войти|продолжить|далее",
                        re.I,
                    )
                ),
            ]
        )

        if submit is not None:
            await submit.click()

        await page.wait_for_timeout(1800)

    # На разных вариантах страницы
    # QR может быть показан сразу либо
    # находиться под "Другой способ".
    qr_button = await _first_visible(
        [
            page.get_by_text(
                re.compile(
                    r"QR.?код|по QR|QR",
                    re.I,
                )
            ),
            page.locator(
                'button:has-text("QR")'
            ),
            page.locator(
                'a:has-text("QR")'
            ),
        ]
    )

    if qr_button is None:
        other = await _first_visible(
            [
                page.get_by_text(
                    re.compile(
                        r"другой способ|"
                        r"другим способом|"
                        r"способы входа",
                        re.I,
                    )
                )
            ]
        )

        if other is not None:
            try:
                await other.click()
                await page.wait_for_timeout(
                    800
                )
            except Exception:
                pass

        qr_button = await _first_visible(
            [
                page.get_by_text(
                    re.compile(
                        r"QR.?код|по QR|QR",
                        re.I,
                    )
                ),
                page.locator(
                    'button:has-text("QR")'
                ),
                page.locator(
                    'a:has-text("QR")'
                ),
            ]
        )

    if qr_button is not None:
        try:
            await qr_button.click()
            await page.wait_for_timeout(
                1200
            )
        except Exception:
            pass


async def _editor_authorized(
    page,
    comments_url: str,
) -> bool:
    try:
        await page.goto(
            comments_url,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        await page.wait_for_timeout(
            2500
        )
    except Exception:
        return False

    url = page.url.lower()

    if "passport.yandex" in url:
        return False

    if "/profile/editor/" not in url:
        return False

    if "/comments" not in url:
        return False

    body = ""

    try:
        body = (
            await page.locator(
                "body"
            ).inner_text()
        ).lower()
    except Exception:
        pass

    if (
        "войти в аккаунт" in body
        or "авторизуйтесь" in body
    ):
        return False

    return True


async def authorize_tenant_dzen_by_qr(
    *,
    bot: Bot,
    user_id: int,
    yandex_login: str,
    comments_url: str,
    profile_dir: str,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    async with _lock(user_id):
        profile = Path(profile_dir)
        profile.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with async_playwright() as pw:
            context = (
                await pw.chromium
                .launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=True,
                    viewport={
                        "width": 1200,
                        "height": 900,
                    },
                    locale="ru-RU",
                )
            )

            try:
                page = (
                    context.pages[0]
                    if context.pages
                    else await context.new_page()
                )

                # Возможно, профиль уже
                # авторизован с прошлого раза.
                if await _editor_authorized(
                    page,
                    comments_url,
                ):
                    await tenant_db.set_tenant_dzen_enabled(
                        user_id,
                        True,
                    )

                    return {
                        "status": "already_authorized",
                    }

                await _open_qr_login(
                    page,
                    yandex_login,
                )

                await page.wait_for_timeout(
                    1200
                )

                screenshot = await page.screenshot(
                    full_page=True,
                    type="png",
                )

                await bot.send_photo(
                    user_id,
                    BufferedInputFile(
                        screenshot,
                        filename="yandex_qr_login.png",
                    ),
                    caption=(
                        "🔐 <b>Авторизация Яндекс</b>\n\n"
                        "Откройте приложение Яндекс ID "
                        "или Яндекс на телефоне и "
                        "отсканируйте QR-код.\n\n"
                        "Пароль боту отправлять не нужно.\n"
                        "QR действует ограниченное время."
                    ),
                    parse_mode="HTML",
                )

                await bot.send_message(
                    user_id,
                    "⏳ Жду подтверждения входа…",
                )

                deadline = (
                    asyncio.get_running_loop().time()
                    + max(
                        60,
                        int(timeout_seconds),
                    )
                )

                while (
                    asyncio.get_running_loop().time()
                    < deadline
                ):
                    await asyncio.sleep(3)

                    # После подтверждения QR
                    # Яндекс меняет страницу/сессию.
                    if "passport.yandex" in (
                        page.url.lower()
                    ):
                        continue

                    if await _editor_authorized(
                        page,
                        comments_url,
                    ):
                        await tenant_db.set_tenant_dzen_enabled(
                            user_id,
                            True,
                        )

                        await bot.send_message(
                            user_id,
                            "✅ <b>Дзен подключён.</b>\n\n"
                            "Автоответы активированы. "
                            "Лимит — до 3 подтверждённых "
                            "ответов в сутки.",
                            parse_mode="HTML",
                        )

                        log.info(
                            "Tenant Dzen QR auth success: "
                            "user=%s",
                            user_id,
                        )

                        return {
                            "status": "authorized",
                        }

                await tenant_db.set_tenant_dzen_enabled(
                    user_id,
                    False,
                )

                await bot.send_message(
                    user_id,
                    "⌛ Время авторизации истекло.\n"
                    "Запустите подключение Дзена ещё раз.",
                )

                return {
                    "status": "timeout",
                }

            except Exception as exc:
                await tenant_db.set_tenant_dzen_enabled(
                    user_id,
                    False,
                )

                log.exception(
                    "Tenant Dzen QR auth error: user=%s",
                    user_id,
                )

                try:
                    await bot.send_message(
                        user_id,
                        "❌ Не удалось выполнить "
                        "авторизацию Дзена. "
                        "Попробуйте ещё раз.",
                    )
                except Exception:
                    pass

                return {
                    "status": "error",
                    "error": str(exc),
                }

            finally:
                await context.close()
