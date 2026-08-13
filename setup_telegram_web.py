from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


async def first_visible(locators):
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


async def authorized(page) -> bool:
    marker = await first_visible(
        [
            page.locator(
                '.chatlist, .chatlist-container, .sidebar-left'
            ),
            page.locator(
                'input[placeholder*="Search" i]'
            ),
            page.locator(
                'input[placeholder*="Поиск" i]'
            ),
            page.locator(
                '.input-search-input'
            ),
        ]
    )
    return marker is not None


async def phone_login(page) -> None:
    phone_button = await first_visible(
        [
            page.get_by_text(
                re.compile(
                    r"log in by phone|войти по номеру",
                    re.I,
                )
            ),
            page.get_by_role(
                "button",
                name=re.compile(
                    r"phone|номер",
                    re.I,
                ),
            ),
        ]
    )

    if phone_button is not None:
        await phone_button.click()
        await page.wait_for_timeout(500)

    phone_input = await first_visible(
        [
            page.locator(
                'input[type="tel"]'
            ),
            page.locator(
                'input[name*="phone" i]'
            ),
        ]
    )

    if phone_input is None:
        raise RuntimeError(
            "Не найдено поле номера телефона. "
            "Попробуйте: python setup_telegram_web.py --qr"
        )

    phone = os.getenv(
        "TG_LOGIN_PHONE",
        "",
    ).strip()

    if not phone:
        phone = input(
            "Введите номер Telegram в международном формате, "
            "например +79991234567: "
        ).strip()

    await phone_input.fill(
        phone
    )

    next_button = await first_visible(
        [
            page.get_by_role(
                "button",
                name=re.compile(
                    r"next|далее",
                    re.I,
                ),
            ),
            page.get_by_text(
                re.compile(
                    r"^next$|^далее$",
                    re.I,
                )
            ),
        ]
    )

    if next_button is None:
        raise RuntimeError(
            "Не найдена кнопка Next/Далее."
        )

    await next_button.click()
    await page.wait_for_timeout(
        1800
    )

    print()
    print(
        "Telegram должен прислать код входа в ваш существующий Telegram."
    )
    code = input(
        "Введите код входа: "
    ).strip().replace(
        " ",
        "",
    )

    code_input = await first_visible(
        [
            page.locator(
                'input[autocomplete="one-time-code"]'
            ),
            page.locator(
                'input[name*="code" i]'
            ),
            page.locator(
                '.input-field input'
            ),
        ]
    )

    if code_input is not None:
        await code_input.fill(
            code
        )
    else:
        # В некоторых версиях Telegram Web код вводится в набор полей
        # или активный кастомный input.
        await page.keyboard.type(
            code,
            delay=80,
        )

    await page.wait_for_timeout(
        2200
    )

    if await authorized(page):
        return

    password_input = await first_visible(
        [
            page.locator(
                'input[type="password"]'
            ),
            page.locator(
                'input[name*="password" i]'
            ),
        ]
    )

    if password_input is not None:
        password = getpass.getpass(
            "Введите пароль двухэтапной аутентификации Telegram: "
        )

        await password_input.fill(
            password
        )

        submit = await first_visible(
            [
                page.get_by_role(
                    "button",
                    name=re.compile(
                        r"next|далее|submit|войти",
                        re.I,
                    ),
                ),
            ]
        )

        if submit is not None:
            await submit.click()
        else:
            await page.keyboard.press(
                "Enter"
            )

        await page.wait_for_timeout(
            2500
        )


async def qr_login(page, profile_dir: Path) -> None:
    print()
    print("Режим QR.")
    print(
        "Скрипт будет обновлять screenshot страницы каждые 3 секунды."
    )
    print(
        "Файл: data/telegram_login_qr.png"
    )
    print(
        "Откройте этот PNG через SFTP/файловый менеджер Beget "
        "и отсканируйте QR в Telegram → Настройки → Устройства."
    )
    print()

    screenshot = (
        BASE_DIR
        / "data"
        / "telegram_login_qr.png"
    )
    screenshot.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    for _ in range(120):
        if await authorized(page):
            return

        await page.screenshot(
            path=str(screenshot),
            full_page=True,
        )
        await page.wait_for_timeout(
            3000
        )

    raise RuntimeError(
        "QR-авторизация не завершена за 6 минут."
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--qr",
        action="store_true",
        help="Использовать QR вместо входа по номеру телефона.",
    )
    args = parser.parse_args()

    profile_raw = os.getenv(
        "TG_WEB_PROFILE_DIR",
        "data/telegram_web_profile",
    ).strip()

    profile_dir = Path(profile_raw)
    if not profile_dir.is_absolute():
        profile_dir = BASE_DIR / profile_dir

    profile_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    web_url = os.getenv(
        "TG_WEB_URL",
        "https://web.telegram.org/k/",
    ).strip()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="ru-RU",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        page.set_default_timeout(
            20000
        )

        print(
            f"Открываю {web_url}"
        )

        await page.goto(
            web_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(
            2500
        )

        if await authorized(page):
            print(
                "Telegram Web уже авторизован. Повторный вход не нужен."
            )
            await context.close()
            return

        try:
            if args.qr:
                await qr_login(
                    page,
                    profile_dir,
                )
            else:
                await phone_login(
                    page
                )
        except Exception:
            debug = (
                BASE_DIR
                / "data"
                / "telegram_setup_error.png"
            )
            debug.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            await page.screenshot(
                path=str(debug),
                full_page=True,
            )
            print(
                f"Сохранён screenshot ошибки: {debug}"
            )
            raise

        for _ in range(20):
            if await authorized(page):
                print()
                print(
                    "Готово: Telegram Web авторизован."
                )
                print(
                    f"Профиль сохранён в: {profile_dir}"
                )
                print(
                    "Теперь выполните: python check_telegram_web.py"
                )
                await context.close()
                return

            await page.wait_for_timeout(
                500
            )

        raise RuntimeError(
            "Вход завершён, но Telegram Web не перешёл к списку чатов."
        )


if __name__ == "__main__":
    asyncio.run(main())
