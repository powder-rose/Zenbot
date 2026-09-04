from __future__ import annotations

from ai_usage import usage_context
from yandex_gpt import ContentBlockedError

import asyncio
import logging
import re
from difflib import SequenceMatcher
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
    clean_short_article_text,
)
from config import Config
from image_gen import YandexArtClient
from search import YandexSearchClient
from yandex_gpt import (
    ARTICLE_SYSTEM_PROMPT,
    SYNCBOT_SYSTEM_PROMPT,
    YandexGPTClient,
)

log = logging.getLogger(__name__)

TENANT_DAILY_ARTICLE_LIMIT = 5
TENANT_DAILY_POPULAR_ARTICLE_LIMIT = 1
POPULAR_COMMENT_TRIGGER = "popular_comment"


# Слова, которые почти не несут смысловой нагрузки
# при сравнении заголовков.
_TITLE_STOPWORDS = {
    "как",
    "что",
    "это",
    "этот",
    "эта",
    "эти",
    "того",
    "для",
    "при",
    "или",
    "если",
    "после",
    "перед",
    "через",
    "между",
    "также",
    "нужно",
    "можно",
    "правильно",
    "вопрос",
    "вопросы",
    "вопросам",
}


def _normalize_article_title(
    value: str,
) -> list[str]:
    """
    Приводит заголовок к набору значимых слов.

    Нужен не для лингвистически идеального анализа,
    а для защиты от очевидно похожих статей подряд.
    """
    value = (
        str(value or "")
        .lower()
        .replace("ё", "е")
    )

    words = re.findall(
        r"[a-zа-я0-9]+",
        value,
        flags=re.IGNORECASE,
    )

    return [
        word
        for word in words
        if len(word) >= 4
        and word not in _TITLE_STOPWORDS
    ]


def _article_title_similarity(
    first: str,
    second: str,
) -> float:
    """
    Возвращает приблизительную похожесть 0..1.

    Учитываем:
      1. схожесть текста целиком;
      2. долю общих значимых слов.

    Берём максимальный показатель.
    """
    first_words = _normalize_article_title(
        first
    )

    second_words = _normalize_article_title(
        second
    )

    if not first_words or not second_words:
        return 0.0

    first_text = " ".join(first_words)
    second_text = " ".join(second_words)

    sequence_score = SequenceMatcher(
        None,
        first_text,
        second_text,
    ).ratio()

    first_set = set(first_words)
    second_set = set(second_words)

    common = len(
        first_set & second_set
    )

    smallest = min(
        len(first_set),
        len(second_set),
    )

    containment_score = (
        common / smallest
        if smallest
        else 0.0
    )

    # Чтобы 1-2 случайных общих слова
    # не давали ложного совпадения.
    if common < 3:
        containment_score = 0.0

    return max(
        sequence_score,
        containment_score,
    )


def _find_similar_article_title(
    candidate: str,
    recent_titles: list[str],
    *,
    threshold: float = 0.58,
) -> tuple[str | None, float]:
    """
    Ищет среди последних публикаций статью,
    слишком похожую на новый кандидат.

    Возвращает:
        (похожий заголовок, score)

    Если совпадения нет:
        (None, лучший score)
    """
    best_title = None
    best_score = 0.0

    for old_title in recent_titles:
        score = _article_title_similarity(
            candidate,
            old_title,
        )

        if score > best_score:
            best_score = score
            best_title = old_title

    if best_score >= threshold:
        return (
            best_title,
            best_score,
        )

    return (
        None,
        best_score,
    )



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


    async def _select_auto_subtopic(
        self,
        user_id: int,
        topic_id: int,
        topic_title: str,
    ) -> str | None:
        """
        Выбирает подтему для обычной плановой статьи.

        Защита от повторов:
        - учитываем историю подтем;
        - учитываем последние 15 опубликованных статей;
        - локально проверяем похожесть;
        - при совпадении просим GPT выбрать другую тему.
        """
        try:
            search_query = (
                f"{topic_title} "
                "актуальные изменения "
                "новые требования практика"
            )

            with usage_context(
                "search_subtopic",
                user_id=user_id,
                metadata={
                    "topic": topic_title,
                },
            ):
                sources = await self.search.search(
                    search_query,
                    max_results=12,
                )

            if not sources:
                return None


            # ----------------------------------------
            # Ранее использованные подтемы
            # конкретного parent topic.
            # ----------------------------------------

            used_subtopics = (
                await tenant_db.list_used_subtopics(
                    user_id,
                    topic_title,
                    limit=80,
                )
            )


            # ----------------------------------------
            # Последние опубликованные статьи
            # пользователя независимо от parent topic.
            # ----------------------------------------

            recent_titles = (
                await tenant_db
                .list_recent_article_titles(
                    user_id,
                    limit=15,
                )
            )


            # GPT сразу видит не только старые подтемы,
            # но и последние реальные заголовки.
            used_for_gpt = list(
                dict.fromkeys(
                    [
                        *used_subtopics,
                        *recent_titles,
                    ]
                )
            )


            # ----------------------------------------
            # До четырёх попыток подобрать
            # действительно отличающуюся подтему.
            # ----------------------------------------

            for attempt in range(1, 5):

                with usage_context(
                    "subtopic_select",
                    user_id=user_id,
                    metadata={
                        "topic": topic_title,
                        "attempt": attempt,
                    },
                ):
                    subtopic = (
                        await self.gpt
                        .select_article_subtopic(
                            topic=topic_title,
                            sources=sources,
                            used_subtopics=used_for_gpt,
                        )
                    )

                subtopic = " ".join(
                    str(subtopic or "").split()
                )

                if not subtopic:
                    log.info(
                        "Tenant: пустая подтема "
                        "user=%s parent=%s attempt=%s",
                        user_id,
                        topic_title,
                        attempt,
                    )
                    continue


                # Исходная parent-тема тоже
                # не считается новой подтемой.
                if (
                    subtopic.casefold()
                    == topic_title.casefold()
                ):
                    log.info(
                        "Tenant: GPT вернул parent topic "
                        "user=%s topic=%s attempt=%s",
                        user_id,
                        topic_title,
                        attempt,
                    )

                    used_for_gpt.append(
                        subtopic
                    )

                    continue


                # ------------------------------------
                # Локальная защита от семантически
                # близких последних публикаций.
                # ------------------------------------

                similar_title, similarity = (
                    _find_similar_article_title(
                        subtopic,
                        recent_titles,
                        threshold=0.58,
                    )
                )


                if similar_title is not None:

                    log.warning(
                        "Tenant similar subtopic rejected: "
                        "user=%s attempt=%s score=%.3f "
                        "candidate=%r previous=%r",
                        user_id,
                        attempt,
                        similarity,
                        subtopic,
                        similar_title,
                    )

                    # На следующей попытке GPT
                    # явно увидит отклонённый вариант
                    # среди уже использованных.
                    used_for_gpt.append(
                        subtopic
                    )

                    continue


                log.info(
                    "Tenant плановая подтема: "
                    "user=%s parent=%s "
                    "subtopic=%s similarity=%.3f "
                    "attempt=%s",
                    user_id,
                    topic_title,
                    subtopic,
                    similarity,
                    attempt,
                )

                return subtopic


            # Если четыре раза получили повторы,
            # не принимаем плохой кандидат.
            log.warning(
                "Tenant: не удалось подобрать "
                "достаточно отличающуюся подтему "
                "за 4 попытки: "
                "user=%s parent=%s",
                user_id,
                topic_title,
            )

            return None


        except Exception:
            log.exception(
                "Tenant: не удалось выбрать "
                "подтему user=%s topic=%s. "
                "Использую исходную тему.",
                user_id,
                topic_title,
            )

            return None


    async def _generate(
        self,
        user_id: int,
        topic: str,
        subtopic: str | None = None,
    ) -> tuple[str, str, str, bytes]:
        focus_topic = (
            subtopic or topic
        ).strip()

        with usage_context(
            "search_article",
            user_id=user_id,
            metadata={
                "topic": topic,
                "subtopic": subtopic,
            },
        ):
            sources = await self.search.search(
                focus_topic,
                max_results=8,
            )
        article_prompt = (
            await tenant_db.get_setting(
                user_id,
                "prompt_article_system",
                "",
            )
        ).strip() or ARTICLE_SYSTEM_PROMPT

        custom_short_prompt = (
            await tenant_db.get_setting(
                user_id,
                "prompt_short_system",
                "",
            )
        ).strip()

        short_prompt = (
            custom_short_prompt
            if custom_short_prompt
            else SYNCBOT_SYSTEM_PROMPT
        )

        image_template = (
            await tenant_db.get_setting(user_id, "prompt_image_template", "")
        ).strip() or DEFAULT_IMAGE_PROMPT_TEMPLATE

        with usage_context(
            "article_full",
            user_id=user_id,
            metadata={
                "topic": topic,
                "subtopic": subtopic,
            },
        ):
            title, body = await self.gpt.generate_article_from_sources(
                topic=topic,
                sources=sources,
                subtopic=subtopic,
                max_chars=3200,
                system_prompt=article_prompt,
            )
        title = clean_article_text(title)

        # LONG body сохраняет оформление,
        # указанное пользовательским промптом.
        body = clean_short_article_text(body)

        # SHORT создаётся строго из уже готовой LONG-статьи.
        # Пользовательский prompt_short_system имеет абсолютный
        # приоритет и не смешивается со стандартным fallback.
        with usage_context(
            "article_short",
            user_id=user_id,
            metadata={
                "topic": topic,
                "subtopic": subtopic,
            },
        ):
            _, short_body = (
                await self.gpt
                .generate_syncbot_article_from_article(
                    topic=topic,
                    article_title=title,
                    article_body=body,
                    max_chars=820,
                    system_prompt=short_prompt,
                )
            )

        short_body = clean_short_article_text(short_body)

        log.info(
            "Tenant short article generated: "
            "user=%s prompt=%s chars=%s",
            user_id,
            (
                "CUSTOM"
                if custom_short_prompt
                else "DEFAULT"
            ),
            len(short_body),
        )

        image_prompt = build_image_prompt(
            focus_topic,
            image_template,
        )

        with usage_context(
            "image_generation",
            user_id=user_id,
            metadata={
                "topic": topic,
                "subtopic": subtopic,
            },
        ):
            image_bytes = await self.art.generate_image(
                image_prompt
            )
        if not image_bytes:
            raise RuntimeError("YandexART вернул пустое изображение")
        return title, body, short_body, image_bytes

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
        discover_subtopic: bool = False,
    ) -> dict[str, Any]:
        if not await tenant_db.is_subscription_active(user_id):
            if topic_id is not None:
                await tenant_db.release_topic(user_id, topic_id)
            return {"status": "subscription_inactive", "topic": topic_title}

        # 5 обычных статей за календарные сутки.
        # popular_comment имеет отдельный лимит:
        # 1 статья за календарные сутки.
        is_popular_comment = (
            trigger == POPULAR_COMMENT_TRIGGER
        )

        if is_popular_comment:
            daily_limit = (
                TENANT_DAILY_POPULAR_ARTICLE_LIMIT
            )

            limit_status = (
                "daily_popular_article_limit"
            )

            used_today = await (
                tenant_db.successful_publications_today(
                    user_id,
                    self.cfg.timezone,
                    trigger_type=POPULAR_COMMENT_TRIGGER,
                )
            )

        else:
            daily_limit = (
                TENANT_DAILY_ARTICLE_LIMIT
            )

            limit_status = (
                "daily_article_limit"
            )

            used_today = await (
                tenant_db.successful_publications_today(
                    user_id,
                    self.cfg.timezone,
                    exclude_trigger_type=POPULAR_COMMENT_TRIGGER,
                )
            )

        if used_today >= daily_limit:
            if topic_id is not None:
                await tenant_db.release_topic(
                    user_id,
                    topic_id,
                )

            log.info(
                "Tenant daily article limit: "
                "user=%s trigger=%s used=%s limit=%s",
                user_id,
                trigger,
                used_today,
                daily_limit,
            )

            return {
                "status": limit_status,
                "topic": topic_title,
                "used": used_today,
                "limit": daily_limit,
            }

        channels = await tenant_db.list_channels(user_id)
        if not channels:
            if topic_id is not None:
                await tenant_db.release_topic(user_id, topic_id)
            return {"status": "no_channel", "topic": topic_title}

        selected_subtopic = None

        if (
            discover_subtopic
            and topic_id is not None
        ):
            selected_subtopic = (
                await self._select_auto_subtopic(
                    user_id,
                    topic_id,
                    topic_title,
                )
            )

        try:
            title, body, short_body, image_bytes = (
                await self._generate(
                    user_id,
                    topic_title,
                    subtopic=selected_subtopic,
                )
            )
        except ContentBlockedError as exc:
            if topic_id is not None:
                await tenant_db.mark_topic_used(
                    user_id,
                    topic_id,
                )

            log.warning(
                "Tenant topic blocked: "
                "user=%s topic=%s",
                user_id,
                topic_title,
            )

            return {
                "status": "content_blocked",
                "topic": topic_title,
                "error": str(exc),
            }

        except Exception as exc:
            if topic_id is not None:
                await tenant_db.release_topic(user_id, topic_id)
            log.exception("Ошибка генерации tenant user=%s topic=%s", user_id, topic_title)
            return {"status": "generation_error", "topic": topic_title, "error": str(exc)}

        # ----------------------------------------
        # Финальная защита от похожих статей.
        #
        # Даже если подтема прошла проверку,
        # GPT может сформулировать итоговый заголовок
        # слишком похоже на недавнюю публикацию.
        # ----------------------------------------

        recent_titles = (
            await tenant_db.list_recent_article_titles(
                user_id,
                limit=15,
            )
        )

        similar_title, title_similarity = (
            _find_similar_article_title(
                title,
                recent_titles,
                threshold=0.58,
            )
        )

        if similar_title is not None:
            log.warning(
                "Tenant final title rejected: "
                "user=%s score=%.3f "
                "candidate=%r previous=%r",
                user_id,
                title_similarity,
                title,
                similar_title,
            )

            if topic_id is not None:
                await tenant_db.release_topic(
                    user_id,
                    topic_id,
                )

            return {
                "status": "similar_article_skipped",
                "topic": topic_title,
                "article_title": title,
                "similar_to": similar_title,
                "similarity": round(
                    title_similarity,
                    3,
                ),
            }

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

        published, errors = await self._publish_channels(
            user_id,
            title,
            short_body,
            image_bytes,
        )
        if published > 0:
            await tenant_db.finish_publication(
                publication_id,
                status="published",
                channels_published=published,
                error="; ".join(errors)[:2000] if errors else None,
            )

            if selected_subtopic:
                try:
                    await tenant_db.record_used_subtopic(
                        user_id,
                        topic_id,
                        topic_title,
                        selected_subtopic,
                    )
                except Exception:
                    log.exception(
                        "Tenant: не удалось "
                        "сохранить подтему "
                        "user=%s subtopic=%s",
                        user_id,
                        selected_subtopic,
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

    async def publish_random_topic(
        self,
        user_id: int,
        trigger: str = "auto",
    ) -> dict[str, Any]:
        """
        Публикует случайную тему.

        Для auto-публикации:
        если итоговая статья оказалась слишком похожей
        на недавнюю, берём другую базовую тему.

        Максимум 4 темы на один временной слот.
        """
        async with self._lock(user_id):

            if not await tenant_db.is_subscription_active(
                user_id
            ):
                return {
                    "status": "subscription_inactive",
                }

            max_attempts = (
                4
                if trigger == "auto"
                else 1
            )

            last_result = None


            for attempt in range(
                1,
                max_attempts + 1,
            ):

                topic = await tenant_db.reserve_random_topic(
                    user_id
                )

                if topic is None:

                    if last_result is not None:
                        result = dict(
                            last_result
                        )

                        result[
                            "attempts"
                        ] = attempt - 1

                        return result

                    return {
                        "status": "no_topics",
                    }


                topic_id = int(
                    topic["id"]
                )

                topic_title = str(
                    topic["title"]
                )

                priority = int(
                    topic.get(
                        "priority",
                        0,
                    )
                    or 0
                )


                result = await self._do_publish(
                    user_id,
                    topic_id=topic_id,
                    topic_title=topic_title,
                    trigger=trigger,
                    discover_subtopic=(
                        trigger == "auto"
                        and priority == 0
                    ),
                )


                status = str(
                    result.get(
                        "status",
                        "",
                    )
                )

                # Срочная случайная тема — одноразовая.
                #
                # Если обработка темы уже началась, после этой
                # попытки она полностью исчезает из активных тем:
                # ни в использованные, ни в неиспользованные
                # больше не попадает.
                #
                # Не расходуем её только когда публикация вообще
                # не могла начаться.
                urgent_keep_statuses = {
                    "subscription_inactive",
                    "daily_article_limit",
                    "daily_popular_article_limit",
                    "no_channel",
                }

                if (
                    trigger == "urgent_random"
                    and status not in urgent_keep_statuses
                ):
                    await tenant_db.deactivate_topic(
                        user_id,
                        topic_id,
                    )

                    log.info(
                        "Tenant urgent topic consumed: "
                        "user=%s topic_id=%s title=%r status=%s",
                        user_id,
                        topic_id,
                        topic_title,
                        status,
                    )

                    result["attempts"] = attempt
                    return result


                # ------------------------------------
                # Всё нормально или произошла
                # обычная ошибка — возвращаем результат.
                # ------------------------------------

                if status != "similar_article_skipped":

                    result["attempts"] = attempt

                    return result


                # ------------------------------------
                # Итоговый заголовок оказался
                # похож на недавнюю публикацию.
                #
                # _do_publish уже освободил reserved.
                #
                # Ставим parent topic в конец очереди,
                # чтобы следующая попытка не взяла
                # её тут же снова.
                # ------------------------------------

                log.warning(
                    "Tenant retry after similar article: "
                    "user=%s attempt=%s/%s "
                    "topic=%r candidate=%r similar_to=%r "
                    "score=%s",
                    user_id,
                    attempt,
                    max_attempts,
                    topic_title,
                    result.get(
                        "article_title"
                    ),
                    result.get(
                        "similar_to"
                    ),
                    result.get(
                        "similarity"
                    ),
                )

                await tenant_db.mark_topic_used(
                    user_id,
                    topic_id,
                )

                last_result = result


            # ----------------------------------------
            # Четыре разных parent-topic не дали
            # достаточно отличающейся статьи.
            #
            # Ничего похожего не публикуем.
            # ----------------------------------------

            result = dict(
                last_result or {}
            )

            result.update(
                {
                    "status": (
                        "similar_article_skipped"
                    ),
                    "attempts": max_attempts,
                    "reason": (
                        "Не удалось подобрать "
                        "достаточно отличающуюся "
                        "статью за несколько попыток"
                    ),
                }
            )

            log.warning(
                "Tenant auto slot skipped after "
                "%s similarity attempts: user=%s",
                max_attempts,
                user_id,
            )

            return result


    async def publish_manual_topic(
        self,
        user_id: int,
        topic_title: str,
        *,
        trigger: str = "urgent_manual",
    ) -> dict[str, Any]:
        topic_title = " ".join(
            (topic_title or "").split()
        )

        if not topic_title:
            return {
                "status": "generation_error",
                "error": "Пустая тема",
            }

        async with self._lock(user_id):
            return await self._do_publish(
                user_id,
                topic_id=None,
                topic_title=topic_title,
                trigger=trigger,
            )
