from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import db
from article_service import ArticleService
from config import Config

log = logging.getLogger(__name__)

class AutoPublisher:
    def __init__(self, service: ArticleService, cfg: Config):
        self.service = service
        self.cfg = cfg
        self._stop = asyncio.Event()
        self.tz = ZoneInfo(cfg.timezone)

    def stop(self) -> None:
        self._stop.set()

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.service.bot.send_message(admin_id, text)
            except Exception:
                log.exception("Не удалось уведомить админа %s", admin_id)

    async def run(self) -> None:
        log.info("Планировщик запущен. Часовой пояс: %s", self.cfg.timezone)
        while not self._stop.is_set():
            try:
                if await db.auto_publish_enabled():
                    schedule = await db.list_schedule()
                    now = datetime.now(self.tz)
                    hhmm = now.strftime("%H:%M")
                    today = now.date().isoformat()

                    due = any(
                        row["publish_time"] == hhmm
                        for row in schedule
                    )

                    if due:
                        # Один автоматический пост на каждый конкретный слот.
                        # Например 09:00, 14:00 и 19:00 = три поста в сутки.
                        slot_key = f"{today}|{hhmm}"
                        last_slot = await db.get_setting(
                            "last_auto_slot",
                            "",
                        )

                        if last_slot != slot_key:
                            await db.set_setting(
                                "last_auto_slot",
                                slot_key,
                            )

                            result = await self.service.publish_random_topic(
                                trigger="auto"
                            )

                            if result.get("status") == "no_topics":
                                await self._notify_admins(
                                    "⚠️ Нет активных тем для автоматической статьи."
                                )
                            elif result.get("status") != "ok":
                                await self._notify_admins(
                                    "❌ Ошибка автоматической статьи:\n"
                                    f"{result.get('error', 'неизвестная ошибка')}"
                                )
                            elif result.get("telegram") != "published":
                                await self._notify_admins(
                                    "⚠️ Статья создана, но публикация в Telegram вернула ошибку.\n"
                                    f"Тема: {result.get('topic')}\n"
                                    f"Telegram: {result.get('telegram')}"
                                )

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в цикле планировщика")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
