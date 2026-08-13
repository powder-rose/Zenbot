from __future__ import annotations

import asyncio
import os
from pathlib import Path

from telethon import TelegramClient


async def main() -> None:
    api_id_raw = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    session_path = os.getenv(
        "TG_MT_SESSION",
        "telegram_publisher",
    ).strip()
    channel = os.getenv(
        "TG_MT_CHANNEL",
        "",
    ).strip()

    if not api_id_raw.isdigit():
        raise RuntimeError(
            "В .env / переменных окружения не задан корректный TG_API_ID"
        )

    if not api_hash:
        raise RuntimeError(
            "В .env / переменных окружения не задан TG_API_HASH"
        )

    if not channel:
        raise RuntimeError(
            "В .env / переменных окружения не задан TG_MT_CHANNEL"
        )

    client = TelegramClient(
        str(Path(session_path).expanduser().resolve()),
        int(api_id_raw),
        api_hash,
    )

    print()
    print("Авторизация Telegram MTProto.")
    print("Используйте СВОЙ существующий аккаунт администратора канала.")
    print("Код входа и пароль 2FA вводятся только локально в этом окне.")
    print("Никому не отправляйте файл .session, api_hash, код входа или пароль.")
    print()

    await client.start()

    me = await client.get_me()
    premium = bool(
        getattr(me, "premium", False)
    )

    print(f"Авторизован аккаунт ID: {me.id}")
    print(f"Telegram Premium: {premium}")

    if not premium:
        print()
        print(
            "ВНИМАНИЕ: для media caption около 3000 символов "
            "на этом аккаунте нужен Telegram Premium."
        )

    ref: str | int = channel
    if channel.lstrip("-").isdigit():
        ref = int(channel)

    entity = await client.get_entity(ref)

    print(
        "Канал найден:",
        getattr(entity, "title", channel),
    )
    print()
    print("MTProto-сессия готова.")
    print("Теперь можно запускать: python bot.py")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
