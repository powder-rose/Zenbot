from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import tenant_db
from config import Config
from tenant_service import TenantArticleService

log = logging.getLogger(__name__)


class TenantScheduler:
    def __init__(self, service: TenantArticleService, cfg: Config) -> None:
        self.service = service
        self.cfg = cfg
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        tz = ZoneInfo(self.cfg.timezone)
        log.info("Tenant scheduler запущен, timezone=%s", self.cfg.timezone)
        while not self._stop.is_set():
            try:
                now = datetime.now(tz)
                publish_time = now.strftime("%H:%M")
                slot_key = now.strftime("%Y-%m-%d|%H:%M")
                user_ids = await tenant_db.claim_due_users(publish_time, slot_key)
                for user_id in user_ids:
                    if self._stop.is_set():
                        break
                    try:
                        result = await self.service.publish_random_topic(user_id, trigger="auto")
                        log.info("Tenant auto user=%s result=%s", user_id, result.get("status"))
                    except Exception:
                        log.exception("Tenant scheduler: ошибка user=%s", user_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Tenant scheduler cycle error")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
