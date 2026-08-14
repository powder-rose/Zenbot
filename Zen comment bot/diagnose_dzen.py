"""One-shot diagnostics for Dzen UI selectors.

Creates HTML/screenshot snapshots in data/debug without sending any reply.
"""
from __future__ import annotations

import asyncio
from dotenv import load_dotenv

from config import Settings
from dzen_browser import DzenBrowser

load_dotenv()


async def main() -> None:
    settings = Settings.from_env()
    browser = DzenBrowser(settings)
    try:
        ok, info = await browser.check_auth()
        print("AUTH:", ok, info)
        if not ok:
            return
        comments = await browser.collect_new_comments(20)
        print(f"FOUND: {len(comments)}")
        for c in comments:
            print("-", c.comment_id, repr(c.author), repr(c.text[:160]), c.publication_url, c.source)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
