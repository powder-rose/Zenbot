from __future__ import annotations

import asyncio

# Один persistent Chromium profile нельзя безопасно открывать двумя воркерами
# одновременно. Этот lock общий для всех Dzen Playwright-задач внутри Zenbot.
DZEN_BROWSER_LOCK = asyncio.Lock()
