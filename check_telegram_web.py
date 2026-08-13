from __future__ import annotations

import asyncio
import json

from telegram_web_publisher import TelegramWebPublisher


async def main() -> None:
    publisher = TelegramWebPublisher.from_env()

    try:
        result = await publisher.healthcheck()

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

        if not result["authorized"]:
            raise SystemExit(
                "Telegram Web НЕ авторизован. "
                "Запустите python setup_telegram_web.py"
            )

        print()
        print(
            "OK: Telegram Web профиль готов для headless-публикации."
        )
    finally:
        await publisher.close()


if __name__ == "__main__":
    asyncio.run(main())
