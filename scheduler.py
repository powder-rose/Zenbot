from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
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

    async def _publish_slot(self, slot_time: datetime) -> bool:
        if not await db.auto_publish_enabled():
            return False
        schedule = await db.list_schedule()
        hhmm = slot_time.strftime("%H:%M")
        if not any(row["publish_time"] == hhmm for row in schedule):
            return False
        slot_key = slot_time.strftime("%Y-%m-%d|%H:%M")
        last_slot = await db.get_setting("last_auto_slot", "")
        if last_slot == slot_key or self._stop.is_set():
            return False
        await db.set_setting("last_auto_slot", slot_key)

        try:
            result = await self.service.publish_random_topic(trigger="auto")
        except Exception:
            # A started send can have an unknown outcome. Consume this slot
            # without automatically sending it again, then drain later slots.
            log.exception("Ошибка автоматической статьи: slot=%s", slot_key)
            return True

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
        return True

    async def run(self) -> None:
        log.info("Планировщик запущен. Часовой пояс: %s", self.cfg.timezone)
        pending: deque[datetime] = deque()
        next_minute: datetime | None = None
        minute = timedelta(minutes=1)
        while not self._stop.is_set():
            try:
                now = (
                    datetime.now(self.tz)
                    .astimezone(UTC)
                    .replace(second=0, microsecond=0)
                )
                if next_minute is None:
                    # Start at the current minute, without replaying downtime.
                    next_minute = now
                while next_minute <= now:
                    pending.append(next_minute.astimezone(self.tz))
                    next_minute += minute
                if not pending and next_minute == now + minute:
                    # Recheck the current minute for settings changed while
                    # idle. Existing DB markers prevent duplicate publication.
                    pending.append(now.astimezone(self.tz))
                if pending:
                    attempted = await self._publish_slot(pending[0])
                    pending.popleft()
                    if attempted or pending:
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в цикле планировщика")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
