from __future__ import annotations

from ai_usage import usage_context

import asyncio
import html
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)

import db
from config import Config
from image_gen import YandexArtClient
from search import YandexSearchClient
from telegram_web_publisher import (
    TelegramWebPublisher,
    WebPostRef,
)
from yandex_gpt import (
    ARTICLE_SYSTEM_PROMPT,
    SYNCBOT_SYSTEM_PROMPT,
    YandexGPTClient,
    ContentBlockedError,
)

log = logging.getLogger(__name__)


def normalize_user_rich_markup(text: str) -> str:
    """
    Нормализует пользовательскую разметку.

    Поддерживаем оба варианта:

    [[B]]жирный[[/B]]
    [[I]]курсив[[/I]]
    [[U]]подчёркнутый[[/U]]
    [[S]]зачёркнутый[[/S]]
    [[Q]]цитата[[/Q]]

    и обычный Markdown.
    """
    if not text:
        return ""

    value = str(text)

    replacements = {
        "[[B]]": "<b>",
        "[[/B]]": "</b>",

        "[[I]]": "<i>",
        "[[/I]]": "</i>",

        "[[U]]": "<u>",
        "[[/U]]": "</u>",

        "[[S]]": "<s>",
        "[[/S]]": "</s>",
    }

    for old, new in replacements.items():
        value = value.replace(
            old,
            new,
        )

    # Цитата -> Rich Markdown quote.
    def quote_repl(match):
        content = match.group(1).strip()

        return "\n".join(
            (
                "> " + line
                if line.strip()
                else ">"
            )
            for line in content.splitlines()
        )

    value = re.sub(
        r"\[\[Q\]\](.*?)\[\[/Q\]\]",
        quote_repl,
        value,
        flags=re.I | re.S,
    )

    # Частый вариант модели:
    # [https://site.ru](https://site.ru)
    # превращаем просто в ссылку.
    value = re.sub(
        r"\[(https?://[^\]\s]+)\]\(\1\)",
        r"\1",
        value,
    )

    return value


def clean_short_article_text(text: str) -> str:
    """
    Минимальная очистка SHORT-текста.

    ВАЖНО:
    Markdown и поддерживаемый Telegram Rich HTML
    намеренно сохраняются, чтобы пользовательский
    prompt мог управлять оформлением публикации.
    """
    if not text:
        return ""

    text = normalize_user_rich_markup(
        text
    )

    text = str(text).replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def clean_article_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace(
        "\r\n",
        "\n",
    )

    # Любой Markdown-list со звёздочкой превращаем в обычный bullet.
    text = re.sub(
        r"(?m)^\s*\*+\s+",
        "• ",
        text,
    )
    text = re.sub(
        r"(?m)^\s*-\s+",
        "• ",
        text,
    )

    text = re.sub(
        r"(?m)^\s*#{1,6}\s*",
        "",
        text,
    )

    # Жёсткая страховка: в публикации звёздочек быть не должно.
    text = text.replace(
        "*",
        "",
    )
    text = text.replace(
        "```",
        "",
    )
    text = text.replace(
        "`",
        "",
    )
    text = text.replace(
        "__",
        "",
    )
    text = text.replace(
        "~~",
        "",
    )

    text = re.sub(
        r"<[^>]+>",
        "",
        text,
    )
    text = html.unescape(
        text
    )

    # Нормализуем списки.
    # Если модель написала: "• пункт 1 • пункт 2 • пункт 3",
    # каждый bullet принудительно переносится на отдельную строку.
    text = re.sub(
        r"[ \t]+(?=•\s*)",
        "\n",
        text,
    )

    # Bullet после обычного текста тоже начинаем с новой строки.
    text = re.sub(
        r"(?<!\n)(?<!^)•\s*",
        "\n• ",
        text,
    )

    # Убираем лишние пробелы после bullet.
    text = re.sub(
        r"(?m)^\s*•\s*",
        "• ",
        text,
    )

    # Между пунктами списка должна быть ровно одна строка переноса.
    text = re.sub(
        r"(?m)\n[ \t]*•",
        "\n•",
        text,
    )

    text = re.sub(
        r"\n[ \t]*\n(?:[ \t]*\n)+",
        "\n\n",
        text,
    )

    return text.strip()


MAX_YANDEX_ART_PROMPT_CHARS = 500

DEFAULT_IMAGE_PROMPT_TEMPLATE = (
    "Создай качественную иллюстрацию по теме: {topic}. "
    "Визуально передай основной смысл темы. "
    "Изображение должно быть цельным, понятным "
    "и подходящим для публикации."
)

ALLOWED_BLOG_URL = "https://boykovgroup.ru/blog"


def build_image_prompt(
    topic: str,
    template: str | None = None,
) -> str:
    """
    Формирует prompt для YandexART.

    Пользовательский шаблон имеет абсолютный приоритет.

    Если в пользовательском шаблоне есть {topic},
    маркер заменяется текущей темой.

    Если пользователь удалил {topic}, код ничего
    дополнительно к его prompt не добавляет.

    Если пользовательский prompt отсутствует,
    используется универсальный стандартный шаблон.

    Максимальная длина prompt YandexART — 500 символов.
    """
    custom_template = str(
        template or ""
    ).strip()

    effective_template = (
        custom_template
        if custom_template
        else DEFAULT_IMAGE_PROMPT_TEMPLATE
    )

    clean_topic = " ".join(
        str(topic or "").split()
    )

    if "{topic}" in effective_template:
        result = effective_template.replace(
            "{topic}",
            clean_topic,
        )
    else:
        # ВАЖНО:
        # ничего не дописываем к пользовательскому
        # prompt автоматически.
        result = effective_template

    result = result.strip()

    if len(result) > MAX_YANDEX_ART_PROMPT_CHARS:
        log.warning(
            "YandexART prompt truncated: "
            "chars=%s limit=%s",
            len(result),
            MAX_YANDEX_ART_PROMPT_CHARS,
        )

        result = result[
            :MAX_YANDEX_ART_PROMPT_CHARS
        ].rstrip()

    return result


def enforce_single_blog_link(text: str) -> str:
    """
    В публикации разрешён только один URL:
    https://boykovgroup.ru/blog
    """
    if not text:
        text = ""

    url_pattern = re.compile(
        r"https?://[^\s<>()]+",
        re.I,
    )

    seen_blog = False

    def repl(match: re.Match) -> str:
        nonlocal seen_blog
        raw = match.group(0)
        trimmed = raw.rstrip(
            ".,;:!?)]}"
        )
        tail = raw[len(trimmed):]

        if trimmed.rstrip("/") == ALLOWED_BLOG_URL.rstrip("/"):
            if seen_blog:
                return tail
            seen_blog = True
            return ALLOWED_BLOG_URL + tail

        return ""

    text = url_pattern.sub(
        repl,
        text,
    )

    text = re.sub(
        r"[ \t]{2,}",
        " ",
        text,
    )
    text = re.sub(
        r"\n[ \t]*\n(?:[ \t]*\n)+",
        "\n\n",
        text,
    ).strip()

    if not seen_blog:
        text = (
            f"{text}\n\n"
            f"Больше практических материалов: {ALLOWED_BLOG_URL}"
        ).strip()

    return text

def compose_caption(
    title: str,
    body: str,
    *,
    max_chars: int = 4096,
) -> str:
    title = clean_article_text(
        title
    )
    body = clean_article_text(
        body
    )

    caption = (
        f"{title}\n\n{body}"
    ).strip()

    if len(caption) <= max_chars:
        return caption

    room = (
        max_chars
        - len(title)
        - 2
    )

    shortened = body[
        :max(0, room)
    ].rstrip()

    cut = max(
        shortened.rfind("."),
        shortened.rfind("!"),
        shortened.rfind("?"),
    )

    if cut >= int(
        max(1, room) * 0.80
    ):
        shortened = (
            shortened[:cut + 1]
            .rstrip()
        )
    else:
        shortened = (
            shortened.rsplit(
                " ",
                1,
            )[0]
            .rstrip()
            + "…"
        )

    return (
        f"{title}\n\n{shortened}"
    ).strip()


def _paragraphs(
    text: str,
) -> list[str]:
    cleaned = clean_article_text(
        text
    )

    return [
        block.strip()
        for block in re.split(
            r"\n\s*\n",
            cleaned,
        )
        if block.strip()
    ]


def build_short_rich_message(
    title: str,
    body: str,
    image_bytes: bytes,
) -> InputRichMessage:
    """
    SHORT Rich Message.

    Если пользовательская разметка [[B]] / [[I]] /
    [[U]] / [[S]] уже преобразована в HTML-теги,
    используем чистый Rich HTML.

    Для обычного Markdown сохраняем старый
    Rich Markdown режим.
    """
    clean_title = clean_article_text(
        title
    )

    rich_body = clean_short_article_text(
        body
    )

    image_file = BufferedInputFile(
        image_bytes,
        filename="article.jpg",
    )

    media = [
        InputRichMessageMedia(
            id="article_cover",
            media=InputMediaPhoto(
                media=image_file,
            ),
        )
    ]

    # Наши служебные теги после normalize_user_rich_markup()
    # становятся HTML-тегами. Не смешиваем их с markdown=.
    use_html = bool(
        re.search(
            r"</?(?:b|i|u|s)>",
            rich_body,
            re.I,
        )
    )

    if use_html:
        allowed_tags = (
            "b",
            "/b",
            "i",
            "/i",
            "u",
            "/u",
            "s",
            "/s",
        )

        def escape_preserving_rich_tags(
            value: str,
        ) -> str:
            result = html.escape(
                value,
                quote=False,
            )

            for tag in allowed_tags:
                result = result.replace(
                    f"&lt;{tag}&gt;",
                    f"<{tag}>",
                )

            return result

        blocks = [
            '<img src="tg://photo?id=article_cover"/>',
            (
                "<p><b>"
                + html.escape(clean_title)
                + "</b></p>"
            ),
        ]

        for block in re.split(
            r"\n\s*\n",
            rich_body,
        ):
            block = block.strip()

            if not block:
                continue

            lines = block.splitlines()

            nonempty = [
                line
                for line in lines
                if line.strip()
            ]

            is_quote = (
                bool(nonempty)
                and all(
                    line.lstrip().startswith(">")
                    for line in nonempty
                )
            )

            if is_quote:
                quote_lines = []

                for line in lines:
                    stripped = line.lstrip()

                    if stripped.startswith(">"):
                        stripped = stripped[1:]

                        if stripped.startswith(" "):
                            stripped = stripped[1:]

                    quote_lines.append(
                        escape_preserving_rich_tags(
                            stripped
                        )
                    )

                blocks.append(
                    "<blockquote>"
                    + "<br>".join(quote_lines)
                    + "</blockquote>"
                )

            else:
                safe_lines = [
                    escape_preserving_rich_tags(
                        line
                    )
                    for line in lines
                ]

                blocks.append(
                    "<p>"
                    + "<br>".join(safe_lines)
                    + "</p>"
                )

        article_html = "".join(
            blocks
        )

        return InputRichMessage(
            html=article_html,
            media=media,
        )

    # Fallback для клиентов, которые используют
    # обычный Markdown вместо служебных [[...]].
    article_markdown = (
        "![](tg://photo?id=article_cover)"
        "\n\n"
        f"<b>{html.escape(clean_title)}</b>"
    )

    if rich_body:
        article_markdown += (
            "\n\n"
            + rich_body
        )

    return InputRichMessage(
        markdown=article_markdown,
        media=media,
    )


async def send_short_rich_message(
    bot: Bot,
    chat_id: int | str,
    title: str,
    body: str,
    image_bytes: bytes,
) -> int:
    rich_message = build_short_rich_message(
        title,
        body,
        image_bytes,
    )

    message = await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=rich_message,
    )

    return int(
        message.message_id
    )


class ArticleService:
    def __init__(
        self,
        *,
        bot: Bot,
        cfg: Config,
        gpt_client: YandexGPTClient,
        search_client: YandexSearchClient,
        art_client: YandexArtClient,
    ):
        self.bot = bot
        self.cfg = cfg
        self.gpt = gpt_client
        self.search = search_client
        self.art = art_client
        self.telegram_web = TelegramWebPublisher.from_env()

        self.lock = asyncio.Lock()
        self.background_tasks: set[
            asyncio.Task
        ] = set()

        self.image_dir = (
            cfg.db_path.parent
            / "images"
        )
        self.image_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    async def close(self) -> None:
        await self.telegram_web.close()


    async def _select_auto_subtopic(
        self,
        topic_id: int,
        topic_title: str,
    ) -> str | None:
        """
        Используется ТОЛЬКО для обычной
        плановой публикации.
        """

        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            try:
                current_tz = ZoneInfo(
                    str(
                        getattr(
                            self.cfg,
                            "timezone",
                            "Europe/Moscow",
                        )
                    )
                )
            except Exception:
                current_tz = ZoneInfo(
                    "Europe/Moscow"
                )

            now_local = datetime.now(
                current_tz
            )

            today_text = now_local.strftime(
                "%d.%m.%Y"
            )

            search_query = (
                f"{topic_title} "
                f"актуально на {today_text} "
                f"{now_local.year} "
                "сегодня актуальные изменения "
                "новые требования "
                "будущие изменения "
                "вступит в силу практика"
            )

            with usage_context(
                "search_subtopic",
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

            used = await db.list_used_subtopics(
                topic_title,
                limit=80,
            )

            with usage_context(
                "subtopic_select",
                metadata={"topic": topic_title},
            ):
                subtopic = (
                    await self.gpt.select_article_subtopic(
                        topic=topic_title,
                        sources=sources,
                        used_subtopics=used,
                    )
                )

            subtopic = " ".join(
                str(subtopic).split()
            )

            if not subtopic:
                return None

            if (
                subtopic.casefold()
                == topic_title.casefold()
            ):
                return None

            log.info(
                "Плановая подтема: "
                "parent=%s subtopic=%s",
                topic_title,
                subtopic,
            )

            return subtopic

        except Exception:
            log.exception(
                "Не удалось выбрать плановую "
                "подтему для %s. "
                "Использую исходную тему.",
                topic_title,
            )

            return None


    async def _generate(
        self,
        topic_title: str,
        subtopic: str | None = None,
    ) -> tuple[
        str,
        str,
        str,
        str,
        bytes,
    ]:
        focus_topic = (
            subtopic or topic_title
        ).strip()

        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            current_tz = ZoneInfo(
                str(
                    getattr(
                        self.cfg,
                        "timezone",
                        "Europe/Moscow",
                    )
                )
            )
        except Exception:
            current_tz = ZoneInfo(
                "Europe/Moscow"
            )

        now_local = datetime.now(
            current_tz
        )

        today_text = now_local.strftime(
            "%d.%m.%Y"
        )

        article_search_query = (
            f"{focus_topic} "
            f"актуально на {today_text} "
            f"{now_local.year} "
            "сегодня актуальные требования "
            "будущие изменения "
            "вступит в силу"
        )

        with usage_context(
            "search_article",
            metadata={
                "topic": topic_title,
                "subtopic": subtopic,
            },
        ):
            sources = await self.search.search(
                article_search_query,
                max_results=8,
            )

        custom_article_prompt = (
            await db.get_setting(
                "prompt_article_system",
                "",
            )
        ).strip()

        custom_short_prompt = (
            await db.get_setting(
                "prompt_short_system",
                "",
            )
        ).strip()

        article_system_prompt = (
            custom_article_prompt
            if custom_article_prompt
            else ARTICLE_SYSTEM_PROMPT
        )

        short_system_prompt = (
            custom_short_prompt
            if custom_short_prompt
            else SYNCBOT_SYSTEM_PROMPT
        )

        log.info(
            "Промпты статьи: "
            "LONG=%s (%s chars), "
            "SHORT=%s (%s chars)",
            (
                "CUSTOM"
                if custom_article_prompt
                else "DEFAULT"
            ),
            len(article_system_prompt),
            (
                "CUSTOM"
                if custom_short_prompt
                else "DEFAULT"
            ),
            len(short_system_prompt),
        )

        image_prompt_template = (
            await db.get_setting(
                "prompt_image_template",
                "",
            )
        ).strip() or DEFAULT_IMAGE_PROMPT_TEMPLATE

        # Long-версия: около 3000 символов с пробелами.
        with usage_context(
            "article_full",
            metadata={
                "topic": topic_title,
                "subtopic": subtopic,
            },
        ):
            title, full_body = await self.gpt.generate_article_from_sources(
                topic=focus_topic,
                sources=sources,
                subtopic=subtopic,
                max_chars=3200,
                system_prompt=article_system_prompt,
            )

        # Short-версия: остаётся в Telegram после удаления long-post.
        with usage_context(
            "article_short",
            metadata={"topic": topic_title},
        ):
            short_title, short_body = await self.gpt.generate_syncbot_article_from_article(
                topic=topic_title,
                article_title=title,
                article_body=full_body,
                max_chars=820,
                system_prompt=short_system_prompt,
            )

        title = clean_article_text(
            title
        )

        short_title = clean_article_text(
            short_title
        ) or title
        if custom_article_prompt:
            full_body = clean_short_article_text(
                full_body
            )
        else:
            full_body = clean_article_text(
                full_body
            )
        short_body = clean_short_article_text(short_body)

        # Содержательная постобработка допустима только
        # для стандартных промптов.
        #
        # Пользовательский prompt имеет абсолютный приоритет:
        # не добавляем, не удаляем и не заменяем его CTA/URL.
        if not custom_article_prompt:
            full_body = enforce_single_blog_link(
                full_body
            )

        if not custom_short_prompt:
            short_body = enforce_single_blog_link(
                short_body
            )

        log.info(
            "Тексты готовы: long=%s chars, short=%s chars",
            len(full_body),
            len(short_body),
        )

        prompt = build_image_prompt(
            focus_topic,
            image_prompt_template,
        )

        log.info(
            "YandexART prompt length: %s",
            len(prompt),
        )

        image_bytes: bytes | None = None
        last_error: Exception | None = None

        for attempt in range(
            1,
            3,
        ):
            try:
                log.info(
                    "YandexART: генерация изображения, попытка %s/2",
                    attempt,
                )

                with usage_context(
                    "image_generation",
                    metadata={
                        "topic": topic_title,
                        "subtopic": subtopic,
                    },
                ):
                    image_bytes = await self.art.generate_image(
                        prompt
                    )

                if not image_bytes:
                    raise RuntimeError(
                        "YandexART вернул пустое изображение"
                    )

                if len(image_bytes) < 10_000:
                    raise RuntimeError(
                        "YandexART вернул слишком маленький файл: "
                        f"{len(image_bytes)} bytes"
                    )

                log.info(
                    "YandexART: изображение готово, size=%s bytes",
                    len(image_bytes),
                )
                break

            except Exception as exc:
                last_error = exc
                log.exception(
                    "YandexART: ошибка генерации, попытка %s/2",
                    attempt,
                )

                if attempt < 2:
                    await asyncio.sleep(
                        3
                    )

        if not image_bytes:
            raise RuntimeError(
                "Не удалось получить изображение YandexART "
                "после 2 попыток. "
                f"Последняя ошибка: {last_error}"
            )

        return (
            title,
            full_body,
            short_title,
            short_body,
            image_bytes,
        )

    async def _publish_destinations(
        self,
        publication_id: int,
        title: str,
        full_body: str,
        short_title: str,
        short_body: str,
        image_bytes: bytes | None,
        image_path: str | None,
    ) -> dict[str, Any]:
        result: dict[
            str,
            Any,
        ] = {
            "publication_id": publication_id,
            "telegram": "pending",
        }

        try:
            if not image_bytes:
                raise RuntimeError(
                    "Нет изображения: публикация отменена"
                )

            full_caption = compose_caption(
                title,
                full_body,
                max_chars=4096,
            )

            log.info(
                "Telegram Web: публикую long standard media post "
                "для Синхробота Дзена, chars=%s",
                len(full_caption),
            )

            post_ref = await self.telegram_web.send_media_post(
                image_bytes,
                full_caption,
                silent=True,
            )

            serialized_ref = (
                post_ref.serialize()
            )

            log.info(
                "Telegram Web: long-post опубликован. "
                "Через 180 сек он будет удалён; "
                "short Rich Message будет опубликован отдельно."
            )

            await db.set_telegram_result(
                publication_id,
                "published",
                message_id=serialized_ref[:255],
            )

            async def replace_after_dzen_sync() -> None:
                try:
                    await asyncio.sleep(
                        180
                    )

                    log.info(
                        "Прошло 180 секунд. Удаляю long-post из Telegram "
                        "без редактирования, чтобы статья в Дзене "
                        "осталась полной."
                    )

                    long_deleted = False
                    try:
                        await self.telegram_web.delete_post(
                            post_ref,
                            full_caption,
                        )
                        long_deleted = True
                    except Exception:
                        # Не обрываем весь LONG -> SHORT цикл из-за изменения DOM
                        # Telegram Web. Иначе пользователь остаётся без SHORT-поста.
                        log.exception(
                            "Не удалось удалить long-post. "
                            "Продолжаю и публикую short Rich Message; "
                            "long-post потребуется удалить вручную, если он остался."
                        )

                    await asyncio.sleep(
                        1.5
                    )

                    short_body_clean = clean_short_article_text(
                        short_body
                    )

                    log.info(
                        "Bot API: публикую short Rich Message, chars=%s",
                        len(short_body_clean),
                    )

                    short_message_id = await send_short_rich_message(
                        self.bot,
                        self.cfg.telegram_channel_id,
                        short_title,
                        short_body_clean,
                        image_bytes,
                    )

                    await db.set_telegram_result(
                        publication_id,
                        "published",
                        message_id=str(
                            short_message_id
                        ),
                    )

                    log.info(
                        "Готово: long_deleted=%s; "
                        "short Rich Message опубликован, message_id=%s.",
                        long_deleted,
                        short_message_id,
                    )

                except Exception:
                    log.exception(
                        "Ошибка цикла LONG -> DELETE -> SHORT"
                    )

            task = asyncio.create_task(
                replace_after_dzen_sync()
            )
            self.background_tasks.add(
                task
            )
            task.add_done_callback(
                self.background_tasks.discard
            )

            result["telegram"] = "published"
            result[
                "telegram_replace_scheduled"
            ] = True
            result[
                "telegram_long_format"
            ] = "telegram_web_media_caption"
            result[
                "telegram_short_format"
            ] = "rich_message"

        except Exception as exc:
            log.exception(
                "Ошибка публикации long-post через Telegram Web"
            )

            await db.set_telegram_result(
                publication_id,
                "error",
                error=str(exc)[:1000],
            )

            result["telegram"] = "error"
            result[
                "telegram_error"
            ] = str(exc)

        return result

    async def publish_random_topic(
        self,
        trigger: str = "auto",
    ) -> dict[str, Any]:
        async with self.lock:
            topic = await db.reserve_random_topic()

            if topic is None:
                return {
                    "status": "no_topics",
                }

            topic_id = int(
                topic["id"]
            )
            topic_title = topic[
                "title"
            ]


            selected_subtopic = None

            # Автоподбор подтемы работает
            # ТОЛЬКО для обычной плановой статьи.
            #
            # urgent_random -> без подбора
            # priority=1   -> без подбора
            if (
                trigger == "auto"
                and int(
                    topic.get(
                        "priority",
                        0,
                    )
                    or 0
                ) == 0
            ):
                selected_subtopic = (
                    await self._select_auto_subtopic(
                        topic_id,
                        topic_title,
                    )
                )

            try:
                (
                    title,
                    full_body,
                    short_title,
                    short_body,
                    image_bytes,
                ) = await self._generate(
                    topic_title,
                    subtopic=selected_subtopic,
                )
            except ContentBlockedError as exc:
                await db.mark_topic_used(
                    topic_id
                )

                log.warning(
                    "Тема заблокирована YandexGPT: %s",
                    topic_title,
                )

                return {
                    "status": "content_blocked",
                    "topic": topic_title,
                    "error": str(exc),
                }

            except Exception as exc:
                await db.release_topic(
                    topic_id
                )

                log.exception(
                    "Ошибка генерации статьи"
                )

                return {
                    "status": "generation_error",
                    "topic": topic_title,
                    "error": str(exc),
                }

            image_path = None

            if image_bytes:
                target = (
                    self.image_dir
                    / f"topic_{topic_id}_{publication_safe_stamp()}.jpg"
                )
                target.write_bytes(
                    image_bytes
                )
                image_path = str(
                    target
                )

            await db.mark_topic_used(
                topic_id
            )

            publication_id = await db.create_publication(
                topic_id=topic_id,
                topic_title=topic_title,
                article_title=title,
                article_body=full_body,
                image_path=image_path,
                trigger_type=trigger,
            )

            destinations = await self._publish_destinations(
                publication_id,
                title,
                full_body,
                short_title,
                short_body,
                image_bytes,
                image_path,
            )


            if (
                selected_subtopic
                and destinations.get(
                    "telegram"
                ) == "published"
            ):
                try:
                    await db.record_used_subtopic(
                        topic_id,
                        topic_title,
                        selected_subtopic,
                    )
                except Exception:
                    log.exception(
                        "Не удалось сохранить "
                        "историю подтемы: %s",
                        selected_subtopic,
                    )

            return {
                "status": "ok",
                "topic": topic_title,
                "article_title": title,
                **destinations,
            }

    async def generate_manual_preview(
        self,
        topic_title: str,
    ) -> dict[str, Any]:
        """Generate LONG + SHORT + image without publishing anywhere.

        The generated image is persisted in data/images so the exact preview can be
        approved and published later without paying for a second generation.
        """
        async with self.lock:
            topic_title = " ".join(topic_title.split())
            if not topic_title:
                return {
                    "status": "generation_error",
                    "error": "Пустая тема",
                }

            try:
                title, full_body, short_title, short_body, image_bytes = await self._generate(
                    topic_title
                )
            except Exception as exc:
                log.exception("Ошибка генерации предпросмотра статьи")
                return {
                    "status": "generation_error",
                    "topic": topic_title,
                    "error": str(exc),
                }

            image_path = None
            if image_bytes:
                target = self.image_dir / f"preview_{publication_safe_stamp()}.jpg"
                target.write_bytes(image_bytes)
                image_path = str(target)

            return {
                "status": "preview_ready",
                "topic": topic_title,
                "article_title": title,
                "short_title": short_title,
                "full_body": full_body,
                "short_body": short_body,
                "image_path": image_path,
            }

    async def publish_prepared_manual(
        self,
        *,
        topic_title: str,
        title: str,
        full_body: str,
        short_body: str,
        image_path: str,
        short_title: str | None = None,
        trigger_type: str = "dzen_comment_approved",
    ) -> dict[str, Any]:
        """Publish the exact article previously shown in preview."""
        async with self.lock:
            topic_title = " ".join((topic_title or "").split())
            title = clean_article_text(title or "")
            short_title = clean_article_text(
                short_title or title
            )
            full_body = clean_article_text(full_body or "")
            short_body = clean_short_article_text(
                short_body or ""
            )

            if not topic_title or not title or not full_body or not short_body:
                return {
                    "status": "generation_error",
                    "topic": topic_title,
                    "error": "Предпросмотр повреждён или неполный",
                }

            try:
                image_bytes = Path(image_path).read_bytes()
            except Exception as exc:
                return {
                    "status": "generation_error",
                    "topic": topic_title,
                    "error": f"Не удалось прочитать изображение предпросмотра: {exc}",
                }

            publication_id = await db.create_publication(
                topic_id=None,
                topic_title=topic_title,
                article_title=title,
                article_body=full_body,
                image_path=image_path,
                trigger_type=trigger_type,
            )

            destinations = await self._publish_destinations(
                publication_id,
                title,
                full_body,
                short_title,
                short_body,
                image_bytes,
                image_path,
            )

            return {
                "status": "ok",
                "topic": topic_title,
                "article_title": title,
                **destinations,
            }

    async def publish_manual_topic(
        self,
        topic_title: str,
    ) -> dict[str, Any]:
        async with self.lock:
            topic_title = " ".join(
                topic_title.split()
            )

            if not topic_title:
                return {
                    "status": "generation_error",
                    "error": "Пустая тема",
                }

            try:
                (
                    title,
                    full_body,
                    short_title,
                    short_body,
                    image_bytes,
                ) = await self._generate(
                    topic_title
                )
            except ContentBlockedError as exc:
                log.warning(
                    "Ручная тема заблокирована "
                    "YandexGPT: %s",
                    topic_title,
                )

                return {
                    "status": "content_blocked",
                    "topic": topic_title,
                    "error": str(exc),
                }

            except Exception as exc:
                log.exception(
                    "Ошибка генерации срочной статьи"
                )

                return {
                    "status": "generation_error",
                    "topic": topic_title,
                    "error": str(exc),
                }

            # Срочная тема НЕ сохраняется в таблицу topics.
            # Она используется только для этой конкретной публикации.
            image_path = None

            if image_bytes:
                target = (
                    self.image_dir
                    / f"manual_{publication_safe_stamp()}.jpg"
                )
                target.write_bytes(
                    image_bytes
                )
                image_path = str(
                    target
                )

            publication_id = await db.create_publication(
                topic_id=None,
                topic_title=topic_title,
                article_title=title,
                article_body=full_body,
                image_path=image_path,
                trigger_type="urgent_manual",
            )

            destinations = await self._publish_destinations(
                publication_id,
                title,
                full_body,
                short_title,
                short_body,
                image_bytes,
                image_path,
            )

            return {
                "status": "ok",
                "topic": topic_title,
                "article_title": title,
                **destinations,
            }


def publication_safe_stamp() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
