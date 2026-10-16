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

    async def _publish_due_users(self, publish_time: str, slot_key: str) -> bool:
        unstarted: set[int] = set()
        cancelled = False
        attempted = False
        claim = asyncio.create_task(
            tenant_db.claim_due_users(publish_time, slot_key),
            name="tenant-schedule-claim",
        )
        try:
            # Obtain the committed batch even if shutdown arrives during its
            # database write. Otherwise its unstarted users would be lost.
            while not claim.done():
                try:
                    await asyncio.shield(claim)
                except asyncio.CancelledError:
                    cancelled = True
            user_ids = claim.result()
            unstarted.update(user_ids)
            if cancelled:
                raise asyncio.CancelledError

            for user_id in user_ids:
                if self._stop.is_set():
                    break
                # Once service execution starts its send outcome may be
                # unknown. Keep that user's marker, including on failure.
                unstarted.remove(user_id)
                attempted = True
                try:
                    result = await self.service.publish_random_topic(
                        user_id, trigger="auto"
                    )
                    log.info(
                        "Tenant auto user=%s result=%s", user_id, result.get("status")
                    )
                except Exception:
                    log.exception("Tenant scheduler: ошибка user=%s", user_id)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            if cancelled:
                log.exception("Tenant scheduler: claim failed during cancellation")
            raise
        finally:
            try:
                if unstarted:
                    release = asyncio.create_task(
                        tenant_db.release_unstarted_auto_slots(unstarted, slot_key),
                        name="tenant-schedule-release",
                    )
                    while not release.done():
                        try:
                            await asyncio.shield(release)
                        except asyncio.CancelledError:
                            cancelled = True
                    release.result()
            except Exception:
                if cancelled:
                    log.exception(
                        "Tenant scheduler: slot release failed during cancellation"
                    )
                raise
            finally:
                # Cleanup failures must not turn cancellation into a normal
                # scheduler iteration. Repeated cancellation is preserved too.
                if cancelled:
                    raise asyncio.CancelledError
        return attempted

    async def run(self) -> None:
        tz = ZoneInfo(self.cfg.timezone)
        log.info("Tenant scheduler запущен, timezone=%s", self.cfg.timezone)
        while not self._stop.is_set():
            try:
                now = datetime.now(tz)
                publish_time = now.strftime("%H:%M")
                slot_key = now.strftime("%Y-%m-%d|%H:%M")
                await self._publish_due_users(publish_time, slot_key)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Tenant scheduler cycle error")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
