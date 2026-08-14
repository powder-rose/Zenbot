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
    """
    QR-вход с поддержкой 2FA.

    Отличие v41:
    - не считаем наличие password-input признаком неверного пароля;
    - ждём реальный переход в список чатов;
    - ищем явное сообщение об ошибке;
    - разрешаем до 3 попыток ввода 2FA без повторного QR.
    """
    print()
    print("Режим QR.")
    print("Переключаю Telegram Web на вход по QR-коду...")

    qr_button = await first_visible(
        [
            page.get_by_text(
                re.compile(
                    r"log in by qr code|войти по qr",
                    re.I,
                )
            ),
            page.get_by_role(
                "button",
                name=re.compile(
                    r"qr",
                    re.I,
                ),
            ),
            page.locator('a:has-text("LOG IN BY QR CODE")'),
            page.locator('button:has-text("LOG IN BY QR CODE")'),
        ]
    )

    if qr_button is not None:
        await qr_button.click()
        await page.wait_for_timeout(1200)

    qr_marker = await first_visible(
        [
            page.locator("canvas"),
            page.locator("svg"),
            page.locator('[class*="qr" i]'),
            page.locator('[data-testid*="qr" i]'),
        ]
    )

    if qr_marker is None:
        debug = BASE_DIR / "data" / "telegram_qr_not_found.png"
        debug.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(debug), full_page=True)
        raise RuntimeError(
            "Telegram Web не переключился на QR-форму. "
            f"Screenshot: {debug}"
        )

    screenshot = BASE_DIR / "data" / "telegram_login_qr.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)

    print()
    print("QR-форма открыта. Файл обновляется каждые 2 секунды:")
    print(screenshot)
    print()
    print(
        "Откройте свежий PNG и отсканируйте: "
        "Telegram → Настройки → Устройства → Подключить устройство."
    )
    print()

    password_attempts = 0

    async def explicit_password_error() -> str | None:
        """
        Ищем именно текст ошибки, а не само поле пароля.
        """
        error_locators = [
            page.get_by_text(
                re.compile(
                    r"incorrect password|wrong password|invalid password|"
                    r"неверн.*парол|неправильн.*парол",
                    re.I,
                )
            ),
            page.locator(
                '[class*="error" i]'
            ),
        ]

        for locator in error_locators:
            try:
                count = await locator.count()
            except Exception:
                continue

            for i in range(min(count, 10)):
                item = locator.nth(i)
                try:
                    if not await item.is_visible():
                        continue

                    value = " ".join(
                        (await item.inner_text()).split()
                    )

                    if re.search(
                        r"incorrect password|wrong password|invalid password|"
                        r"неверн.*парол|неправильн.*парол",
                        value,
                        re.I,
                    ):
                        return value
                except Exception:
                    continue

        return None

    async def wait_after_password_submit(
        timeout_seconds: int = 25,
    ) -> tuple[str, str | None]:
        """
        Возвращает:
        ("authorized", None)
        ("error", "текст ошибки")
        ("waiting", None)
        """
        loops = max(1, int(timeout_seconds / 0.5))

        for _ in range(loops):
            if await authorized(page):
                return "authorized", None

            error_text = await explicit_password_error()
            if error_text:
                return "error", error_text

            await page.wait_for_timeout(500)

        return "waiting", None

    for _ in range(180):
        if await authorized(page):
            print()
            print("Telegram Web авторизован.")
            return

        password_input = await first_visible(
            [
                page.locator('input[type="password"]'),
                page.locator('input[name*="password" i]'),
                page.locator('input[placeholder*="password" i]'),
                page.locator('input[placeholder*="парол" i]'),
            ]
        )

        if password_input is not None:
            if password_attempts >= 3:
                error_shot = (
                    BASE_DIR
                    / "data"
                    / "telegram_2fa_error.png"
                )
                await page.screenshot(
                    path=str(error_shot),
                    full_page=True,
                )
                raise RuntimeError(
                    "Превышено 3 попытки ввода пароля 2FA. "
                    f"Screenshot: {error_shot}"
                )

            password_attempts += 1

            print()
            print(
                "QR принят. Telegram запрашивает пароль "
                "двухэтапной аутентификации."
            )
            print(
                f"Попытка {password_attempts}/3."
            )

            password = getpass.getpass(
                "Введите пароль 2FA Telegram: "
            )

            # Telegram Web использует скрытый/stealthy password input,
            # поверх которого расположен div.input-field-password.
            # Обычный locator.click() по input может зависнуть, потому что
            # wrapper перехватывает pointer events. Поэтому кликаем wrapper,
            # затем вводим пароль с клавиатуры. Если wrapper не найден,
            # используем focus() без pointer click.
            password_wrapper = await first_visible(
                [
                    page.locator(".input-field-password"),
                    page.locator('[class*="input-field-password"]'),
                ]
            )

            if password_wrapper is not None:
                try:
                    await password_wrapper.click(
                        timeout=5000,
                        force=True,
                    )
                except Exception:
                    await password_input.focus()
            else:
                await password_input.focus()

            # Очищаем текущее значение через клавиатуру — это надёжнее
            # для Telegram Web, чем fill() по stealthy input.
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(password)
            await page.wait_for_timeout(250)

            submit = await first_visible(
                [
                    page.get_by_role(
                        "button",
                        name=re.compile(
                            r"next|далее|submit|войти",
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

            if submit is not None:
                await submit.click()
            else:
                await page.keyboard.press("Enter")

            print(
                "Пароль отправлен. Жду ответ Telegram..."
            )

            status, error_text = await wait_after_password_submit(
                timeout_seconds=25
            )

            if status == "authorized":
                print()
                print("Telegram Web авторизован после 2FA.")
                return

            if status == "error":
                print()
                print(
                    "Telegram явно сообщил, что пароль не принят:"
                )
                print(error_text or "неверный пароль")
                print(
                    "Повторный QR не нужен — можно попробовать пароль ещё раз."
                )
                await page.wait_for_timeout(500)
                continue

            # Если явной ошибки нет, не делаем ложный вывод.
            # Telegram Web мог ещё не завершить переход.
            print()
            print(
                "Явной ошибки пароля нет, но список чатов пока не появился."
            )
            print(
                "Продолжаю ждать состояние Telegram Web..."
            )
            await page.wait_for_timeout(2000)
            continue

        await page.screenshot(
            path=str(screenshot),
            full_page=True,
        )
        await page.wait_for_timeout(2000)

    final_shot = BASE_DIR / "data" / "telegram_auth_timeout.png"
    await page.screenshot(
        path=str(final_shot),
        full_page=True,
    )
    raise RuntimeError(
        "Авторизация Telegram не завершена за 6 минут. "
        f"Screenshot: {final_shot}"
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
