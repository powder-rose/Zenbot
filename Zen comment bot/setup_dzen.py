from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).expanduser()
STATE = DATA_DIR / "dzen_state.json"
STUDIO = os.getenv("DZEN_STUDIO_URL", "https://dzen.ru/profile/editor/specons")


async def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(locale="ru-RU", viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        await page.goto(STUDIO, wait_until="domcontentloaded", timeout=90000)
        print("\n1. В открывшемся Chromium войдите в Яндекс/Дзен под владельцем канала.")
        print("2. Убедитесь, что открывается Дзен Студия нужного канала.")
        print("3. Вернитесь в терминал и нажмите Enter.\n")
        await asyncio.to_thread(input)
        await context.storage_state(path=str(STATE))
        print(f"Сессия сохранена: {STATE.resolve()}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
