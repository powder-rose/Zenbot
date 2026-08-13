from __future__ import annotations

import asyncio
import os
import platform
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def check_env(name: str) -> tuple[bool, str]:
    value = os.getenv(name, "").strip()
    return bool(value), value


async def main() -> None:
    print("Zen Bot v36 — server check")
    print("=" * 45)
    print("Python:", sys.version.split()[0])
    print("OS:", platform.platform())
    print("Project:", BASE_DIR)
    print()

    required = [
        "TELEGRAM_BOT_TOKEN",
        "ADMIN_IDS",
        "TELEGRAM_CHANNEL_ID",
        "TG_WEB_CHANNEL",
        "YC_FOLDER_ID",
    ]

    failed = False

    for name in required:
        ok, _ = check_env(name)
        print(
            f"{'OK' if ok else 'MISSING'}: {name}"
        )
        failed = failed or not ok

    api_key, _ = check_env("YC_API_KEY")
    sa_key, _ = check_env("YC_SA_KEY_FILE")

    if api_key or sa_key:
        print("OK: Yandex Cloud auth")
    else:
        print("MISSING: YC_API_KEY or YC_SA_KEY_FILE")
        failed = True

    print()

    try:
        import playwright
        print("OK: playwright imported")
    except Exception as exc:
        print("ERROR: playwright import:", exc)
        failed = True

    try:
        from telegram_web_publisher import TelegramWebPublisher

        publisher = TelegramWebPublisher.from_env()
        result = await publisher.healthcheck()
        print("Telegram Web authorized:", result["authorized"])
        print("Telegram Web URL:", result["url"])
        await publisher.close()

        if not result["authorized"]:
            failed = True
    except Exception as exc:
        print("ERROR: Telegram Web healthcheck:", exc)
        failed = True

    print()

    if failed:
        raise SystemExit(
            "Есть ошибки. Исправьте их до запуска systemd."
        )

    print(
        "Все основные проверки пройдены."
    )


if __name__ == "__main__":
    asyncio.run(main())
