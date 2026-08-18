from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

import tenant_db
from article_service import (
    DEFAULT_IMAGE_PROMPT_TEMPLATE,
    build_image_prompt,
    build_short_rich_message,
    clean_article_text,
)
from config import Config
from image_gen import YandexArtClient
from search import YandexSearchClient
from yandex_gpt import ARTICLE_SYSTEM_PROMPT, YandexGPTClient

log = logging.getLogger(__name__)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


class TenantArticleService:
    def __init__(
        self,
        *,
        bot: Bot,
        cfg: Config,
        gpt_client: YandexGPTClient,
        search_client: YandexSearchClient,
        art_client: YandexArtClient,
    ) -> None:
        self.bot = bot
        self.cfg = cfg
        self.gpt = gpt_client
        self.search = search_client
        self.art = art_client
        self.image_dir = cfg.db_path.parent / "tenant_images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def _generate(self, user_id: int, topic: str) -> tuple[str, str, bytes]:
        sources = await self.search.search(topic, max_results=8)
        article_prompt = (
            await tenant_db.get_setting(user_id, "prompt_article_system", "")
        ).strip() or ARTICLE_SYSTEM_PROMPT
        image_template = (
            await tenant_db.get_setting(user_id, "prompt_image_template", "")
        ).strip() or DEFAULT_IMAGE_PROMPT_TEMPLATE

        title, body = await self.gpt.generate_article_from_sources(
            topic=topic,
            sources=sources,
            max_chars=3200,
            system_prompt=article_prompt,
        )
        title = clean_article_text(title)
        body = clean_article_text(body)

        image_prompt = build_image_prompt(topic, image_template)
        image_bytes = await self.art.generate_image(image_prompt)
        if not image_bytes:
            raise RuntimeError("YandexART вернул пустое изображение")
        return title, body, image_bytes

    async def _publish_one(self, chat_id: int, title: str, body: str, image_bytes: bytes) -> int:
        # Основной формат v49: Rich Message с изображением и длинной статьёй.
        try:
            rich = build_short_rich_message(title, body, image_bytes)
            msg = await self.bot.send_rich_message(chat_id=chat_id, rich_message=rich)
            return int(msg.message_id)
        except Exception as rich_exc:
            log.warning("Rich Message недоступен для %s: %s. Использую fallback.", chat_id, rich_exc)

        # Fallback: картинка отдельно + полный текст отдельным сообщением.
        image = BufferedInputFile(image_bytes, filename="article.jpg")
        photo = await self.bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=title[:1024] if title else None,
        )
        text = body.strip()
        if text:
            # Статья в проекте ~3200 символов, но режем безопасно на случай длинного кастомного промпта.
            while text:
                chunk = text[:4000]
                if len(text) > 4000 and "\n" in chunk:
                    cut = chunk.rfind("\n")
                    if cut > 2500:
                        chunk = chunk[:cut]
                await self.bot.send_message(chat_id=chat_id, text=chunk)
                text = text[len(chunk):].lstrip()
        return int(photo.message_id)

    async def _publish_channels(
        self,
        user_id: int,
        title: str,
        body: str,
        image_bytes: bytes,
    ) -> tuple[int, list[str]]:
        channels = await tenant_db.list_channels(user_id)
        published = 0
        errors: list[str] = []
        for channel in channels:
            chat_id = int(channel["chat_id"])
            try:
                await self._publish_one(chat_id, title, body, image_bytes)
                published += 1
            except Exception as exc:
                log.exception("Ошибка публикации tenant user=%s channel=%s", user_id, chat_id)
                errors.append(f"{chat_id}: {exc}")
        return published, errors

    async def _do_publish(
        self,
        user_id: int,
        *,
        topic_id: int | None,
        topic_title: str,
        trigger: str,
    ) -> dict[str, Any]:
        if not await tenant_db.is_subscription_active(user_id):
            if topic_id is not None:
                await tenant_db.release_topic(user_id, topic_id)
            return {"status": "subscription_inactive", "topic": topic_title}

        channels = await tenant_db.list_channels(user_id)
        if not channels:
            if topic_id is not None:
                await tenant_db.release_topic(user_id, topic_id)
            return {"status": "no_channel", "topic": topic_title}

        try:
            title, body, image_bytes = await self._generate(user_id, topic_title)
        except Exception as exc:
            if topic_id is not None:
                await tenant_db.release_topic(user_id, topic_id)
            log.exception("Ошибка генерации tenant user=%s topic=%s", user_id, topic_title)
            return {"status": "generation_error", "topic": topic_title, "error": str(exc)}

        image_path: Path = self.image_dir / f"u{user_id}_{_stamp()}.jpg"
        image_path.write_bytes(image_bytes)

        publication_id = await tenant_db.create_publication(
            user_id=user_id,
            topic_id=topic_id,
            topic_title=topic_title,
            article_title=title,
            article_body=body,
            image_path=str(image_path),
            trigger_type=trigger,
        )

        if topic_id is not None:
            await tenant_db.mark_topic_used(user_id, topic_id)

        published, errors = await self._publish_channels(user_id, title, body, image_bytes)
        if published > 0:
            await tenant_db.finish_publication(
                publication_id,
                status="published",
                channels_published=published,
                error="; ".join(errors)[:2000] if errors else None,
            )
            return {
                "status": "ok",
                "topic": topic_title,
                "article_title": title,
                "publication_id": publication_id,
                "channels_published": published,
            }

        error = "; ".join(errors) or "Не удалось опубликовать ни в один канал"
        await tenant_db.finish_publication(
            publication_id,
            status="error",
            channels_published=0,
            error=error[:2000],
        )
        return {
            "status": "publish_error",
            "topic": topic_title,
            "article_title": title,
            "publication_id": publication_id,
            "channels_published": 0,
            "error": error,
        }

    async def publish_random_topic(self, user_id: int, trigger: str = "auto") -> dict[str, Any]:
        async with self._lock(user_id):
            if not await tenant_db.is_subscription_active(user_id):
                return {"status": "subscription_inactive"}
            topic = await tenant_db.reserve_random_topic(user_id)
            if topic is None:
                return {"status": "no_topics"}
            return await self._do_publish(
                user_id,
                topic_id=int(topic["id"]),
                topic_title=str(topic["title"]),
                trigger=trigger,
            )

    async def publish_manual_topic(self, user_id: int, topic_title: str) -> dict[str, Any]:
        topic_title = " ".join((topic_title or "").split())
        if not topic_title:
            return {"status": "generation_error", "error": "Пустая тема"}
        async with self._lock(user_id):
            return await self._do_publish(
                user_id,
                topic_id=None,
                topic_title=topic_title,
                trigger="urgent_manual",
            )
