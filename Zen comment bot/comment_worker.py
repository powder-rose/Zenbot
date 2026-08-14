from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from config import Settings
from database import Database
from dzen_browser import DzenAuthError, DzenBrowser
from models import ProcessResult
from reply_policy import compose_final_reply
from yandexgpt import YandexGPTClient

log = logging.getLogger(__name__)


class CommentWorker:
    def __init__(self, settings: Settings, db: Database, browser: DzenBrowser, ai: YandexGPTClient):
        self.settings = settings
        self.db = db
        self.browser = browser
        self.ai = ai
        self._stop = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self.last_cycle_at: datetime | None = None
        self.last_error: str = ""

    async def enabled(self) -> bool:
        default = "1" if self.settings.comments_enabled_on_start else "0"
        return (await self.db.get_setting("enabled", default)) == "1"

    async def set_enabled(self, enabled: bool) -> None:
        await self.db.set_setting("enabled", "1" if enabled else "0")

    async def loop(self) -> None:
        while not self._stop.is_set():
            try:
                if await self.enabled():
                    await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Comment worker cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> list[ProcessResult]:
        if self._check_lock.locked():
            return []
        async with self._check_lock:
            self.last_cycle_at = datetime.now()
            self.last_error = ""
            results: list[ProcessResult] = []
            try:
                comments = await self.browser.collect_new_comments(self.settings.max_comments_per_cycle)
            except DzenAuthError as exc:
                self.last_error = str(exc)
                raise

            for comment in comments:
                if await self.db.is_processed(comment.comment_id):
                    continue
                try:
                    comment = await self.browser.enrich_comment_context(comment)
                    decision = await self.ai.generate_reply(comment)
                    if decision.action == "skip":
                        await self.db.save_result(
                            comment_id=comment.comment_id,
                            author=comment.author,
                            comment_text=comment.text,
                            publication_url=comment.publication_url,
                            publication_title=comment.publication_title,
                            action="skip",
                            reason=decision.reason,
                        )
                        results.append(ProcessResult(
                            comment_id=comment.comment_id,
                            author=comment.author,
                            comment_text=comment.text,
                            publication_url=comment.publication_url,
                            action="skip",
                            reason=decision.reason,
                        ))
                        continue

                    if decision.action == "review":
                        await self.db.save_result(
                            comment_id=comment.comment_id,
                            author=comment.author,
                            comment_text=comment.text,
                            publication_url=comment.publication_url,
                            publication_title=comment.publication_title,
                            action="review",
                            ai_reply=decision.reply,
                            reason=decision.reason,
                        )
                        results.append(ProcessResult(
                            comment_id=comment.comment_id,
                            author=comment.author,
                            comment_text=comment.text,
                            publication_url=comment.publication_url,
                            action="review",
                            reply_text=decision.reply,
                            reason=decision.reason,
                        ))
                        continue

                    final_reply = compose_final_reply(
                        decision.reply,
                        comment.comment_id,
                        self.settings.blog_url,
                    )
                    published = False
                    if self.settings.dry_run:
                        log.info("DRY_RUN comment=%s reply=%s", comment.comment_id, final_reply)
                    else:
                        delay = random.randint(
                            min(self.settings.reply_delay_min_seconds, self.settings.reply_delay_max_seconds),
                            max(self.settings.reply_delay_min_seconds, self.settings.reply_delay_max_seconds),
                        )
                        if delay:
                            await asyncio.sleep(delay)
                        published = await self.browser.publish_reply(comment, final_reply)

                    action = "review" if (not self.settings.dry_run and not published) else "reply"
                    reason = decision.reason if action == "reply" else "Не удалось подтвердить публикацию ответа в интерфейсе Дзена"
                    await self.db.save_result(
                        comment_id=comment.comment_id,
                        author=comment.author,
                        comment_text=comment.text,
                        publication_url=comment.publication_url,
                        publication_title=comment.publication_title,
                        action=action,
                        ai_reply=decision.reply,
                        final_reply=final_reply,
                        reason=reason,
                        published=published,
                    )
                    results.append(ProcessResult(
                        comment_id=comment.comment_id,
                        author=comment.author,
                        comment_text=comment.text,
                        publication_url=comment.publication_url,
                        action=action,
                        reply_text=final_reply,
                        reason=reason,
                        published=published,
                    ))
                except Exception as exc:
                    log.exception("Failed to process comment %s", comment.comment_id)
                    self.last_error = str(exc)
                    await self.db.save_result(
                        comment_id=comment.comment_id,
                        author=comment.author,
                        comment_text=comment.text,
                        publication_url=comment.publication_url,
                        publication_title=comment.publication_title,
                        action="review",
                        reason=f"Ошибка обработки: {exc}",
                    )
                    results.append(ProcessResult(
                        comment_id=comment.comment_id,
                        author=comment.author,
                        comment_text=comment.text,
                        publication_url=comment.publication_url,
                        action="review",
                        reason=f"Ошибка обработки: {exc}",
                    ))
            return results
