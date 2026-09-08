from __future__ import annotations

from ai_usage import usage_context

import asyncio
import html
import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
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
from dzen_rich_formatter import (
    DzenRichFormatter,
    parse_dzen_markup,
)
from yandex_gpt import (
    ARTICLE_SYSTEM_PROMPT,
    SYNCBOT_SYSTEM_PROMPT,
    YandexGPTClient,
    ContentBlockedError,
    looks_like_news_first_subtopic,
    topic_explicitly_requests_news,
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



def build_public_short_caption_html(
    title: str,
    short_body: str,
) -> str:
    """
    Финальный Telegram photo caption.

    Это одновременно:
    1. окончательный SHORT для Telegram;
    2. transport-пост для импорта в Dzen.

    Пользовательское rich-formatting сохраняется.
    Видимый размер Telegram caption <= 1024 символов.
    """

    clean_title = " ".join(
        str(title or "").split()
    )

    if not clean_title:
        raise RuntimeError(
            "Пустой заголовок SHORT"
        )

    parsed = parse_dzen_markup(
        short_body
    )

    max_visible = 1024

    available = (
        max_visible
        - len(clean_title)
        - 2
    )

    if available <= 0:
        raise RuntimeError(
            "Заголовок слишком длинный "
            "для Telegram caption"
        )

    selected = []
    remaining = available

    for line_index, line in enumerate(
        parsed.lines
    ):
        separator = (
            1
            if selected
            else 0
        )

        if remaining < separator:
            break

        remaining -= separator

        piece = line[
            :max(0, remaining)
        ]

        was_truncated = (
            len(piece)
            < len(line)
        )

        if was_truncated and piece:
            cut = piece.rfind(
                " "
            )

            if cut >= max(
                0,
                len(piece) - 120,
            ):
                piece = (
                    piece[:cut]
                    .rstrip()
                )

        selected.append(
            (
                line_index,
                piece,
            )
        )

        remaining -= len(
            piece
        )

        if was_truncated:
            break

        if remaining <= 0:
            break

    spans_by_line = {}

    for span in parsed.spans:
        spans_by_line.setdefault(
            span.line,
            [],
        ).append(
            span
        )

    order = (
        "bold",
        "italic",
        "underline",
        "strike",
    )

    open_tag = {
        "bold": "<b>",
        "italic": "<i>",
        "underline": "<u>",
        "strike": "<s>",
    }

    close_tag = {
        "bold": "</b>",
        "italic": "</i>",
        "underline": "</u>",
        "strike": "</s>",
    }

    def render_line(
        line_index: int,
        value: str,
    ) -> str:

        if not value:
            return ""

        styles = [
            set()
            for _ in value
        ]

        for span in spans_by_line.get(
            line_index,
            [],
        ):
            start = max(
                0,
                int(span.start),
            )

            end = min(
                len(value),
                int(span.end),
            )

            for pos in range(
                start,
                end,
            ):
                styles[pos].add(
                    span.style
                )

        result = []
        current = set()

        for pos, char in enumerate(
            value
        ):
            target = styles[pos]

            if target != current:

                for style in reversed(
                    order
                ):
                    if style in current:
                        result.append(
                            close_tag[
                                style
                            ]
                        )

                for style in order:
                    if style in target:
                        result.append(
                            open_tag[
                                style
                            ]
                        )

                current = set(
                    target
                )

            result.append(
                html.escape(
                    char,
                    quote=False,
                )
            )

        for style in reversed(
            order
        ):
            if style in current:
                result.append(
                    close_tag[
                        style
                    ]
                )

        rendered = "".join(
            result
        )

        if (
            line_index in parsed.blockquotes
            and value.strip()
        ):
            rendered = (
                "<blockquote>"
                + rendered
                + "</blockquote>"
            )

        return rendered

    body_html = "\n".join(
        render_line(
            line_index,
            value,
        )
        for line_index, value
        in selected
    ).strip()

    caption = (
        "<b>"
        + html.escape(
            clean_title,
            quote=False,
        )
        + "</b>"
    )

    if body_html:
        caption += (
            "\n\n"
            + body_html
        )

    return caption



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


_GLOBAL_TITLE_STOPWORDS = {
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
    "актуальные",
    "важные",
    "новые",
}


def _normalize_global_article_title(
    value: str,
) -> list[str]:

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
        and word not in _GLOBAL_TITLE_STOPWORDS
    ]


def _global_article_title_similarity(
    first: str,
    second: str,
) -> float:

    first_words = (
        _normalize_global_article_title(
            first
        )
    )

    second_words = (
        _normalize_global_article_title(
            second
        )
    )

    if not first_words or not second_words:
        return 0.0

    first_text = " ".join(
        first_words
    )

    second_text = " ".join(
        second_words
    )

    sequence_score = SequenceMatcher(
        None,
        first_text,
        second_text,
    ).ratio()

    first_set = set(
        first_words
    )

    second_set = set(
        second_words
    )

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

    if common < 3:
        containment_score = 0.0

    return max(
        sequence_score,
        containment_score,
    )


def _find_similar_global_article_title(
    candidate: str,
    recent_titles: list[str],
    *,
    threshold: float = 0.58,
) -> tuple[str | None, float]:

    best_title = None
    best_score = 0.0

    for old_title in recent_titles:

        score = (
            _global_article_title_similarity(
                candidate,
                old_title,
            )
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




def _first_article_paragraph(
    body: str,
) -> str:
    """
    Первый содержательный абзац статьи.

    Игнорируем служебные заголовки, цитату автора
    и CTA, потому что они одинаковы по шаблону
    и не должны влиять на оценку сюжета.
    """
    blocks = [
        block.strip()
        for block in re.split(
            r"\n\s*\n",
            str(body or ""),
        )
        if block.strip()
    ]

    for block in blocks:

        plain = re.sub(
            r"\[\[/?[BIUSQ]\]\]",
            "",
            block,
            flags=re.I,
        )

        plain = re.sub(
            r"<[^>]+>",
            "",
            plain,
        )

        plain = plain.strip()

        lowered = (
            plain
            .casefold()
            .replace("ё", "е")
        )

        if not plain:
            continue

        if (
            plain.startswith("==")
            and plain.endswith("==")
        ):
            continue

        if "мое почтение" in lowered:
            continue

        if "николай бойков" in lowered:
            continue

        if "больше практических материалов" in lowered:
            continue

        if len(plain) < 70:
            continue

        return plain[:900]

    return ""


def _story_roots(
    value: str,
) -> list[str]:

    value = (
        str(value or "")
        .casefold()
        .replace("ё", "е")
    )

    words = re.findall(
        r"[a-zа-я0-9]+",
        value,
        flags=re.I,
    )

    stopwords = {
        "которые",
        "который",
        "которая",
        "этого",
        "этими",
        "также",
        "более",
        "после",
        "перед",
        "будут",
        "нужно",
        "можно",
        "своих",
        "своей",
        "свою",
    }

    result = []

    for word in words:

        if (
            len(word) < 4
            or word in stopwords
        ):
            continue

        result.append(
            word[:6]
        )

    return result


def _story_dates(
    value: str,
) -> set[str]:

    text = (
        str(value or "")
        .casefold()
        .replace("ё", "е")
    )

    months = (
        "января|февраля|марта|апреля|мая|июня|"
        "июля|августа|сентября|октября|ноября|декабря"
    )

    result = set(
        re.findall(
            rf"\b\d{{1,2}}\s+(?:{months})\s+20\d{{2}}\b",
            text,
            flags=re.I,
        )
    )

    result.update(
        re.findall(
            r"\b\d{1,2}[./-]\d{1,2}[./-]20\d{2}\b",
            text,
        )
    )

    return result


def _global_article_story_similarity(
    candidate_title: str,
    candidate_body: str,
    previous_title: str,
    previous_body: str,
) -> float:

    title_score = (
        _global_article_title_similarity(
            candidate_title,
            previous_title,
        )
    )

    candidate_lead = (
        _first_article_paragraph(
            candidate_body
        )
    )

    previous_lead = (
        _first_article_paragraph(
            previous_body
        )
    )

    if not candidate_lead or not previous_lead:
        return title_score

    first_roots = _story_roots(
        candidate_lead
    )

    second_roots = _story_roots(
        previous_lead
    )

    if not first_roots or not second_roots:
        return title_score

    first_text = " ".join(
        first_roots
    )

    second_text = " ".join(
        second_roots
    )

    sequence_score = SequenceMatcher(
        None,
        first_text,
        second_text,
    ).ratio()

    first_set = set(
        first_roots
    )

    second_set = set(
        second_roots
    )

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

    candidate_dates = _story_dates(
        candidate_lead
    )

    previous_dates = _story_dates(
        previous_lead
    )

    same_date = bool(
        candidate_dates
        & previous_dates
    )

    score = max(
        title_score,
        sequence_score,
        containment_score,
    )

    # Одинаковая конкретная дата +
    # заметное совпадение содержания —
    # очень сильный признак одного инфоповода.
    if (
        same_date
        and containment_score >= 0.35
    ):
        score = max(
            score,
            0.90,
        )

    return score


def _find_similar_global_article_story(
    candidate_title: str,
    candidate_body: str,
    recent_articles: list[dict],
    *,
    threshold: float = 0.66,
):
    best_article = None
    best_score = 0.0

    for article in recent_articles:

        score = (
            _global_article_story_similarity(
                candidate_title,
                candidate_body,
                article.get(
                    "article_title",
                    "",
                ),
                article.get(
                    "article_body",
                    "",
                ),
            )
        )

        if score > best_score:
            best_score = score
            best_article = article

    if best_score >= threshold:
        return (
            best_article,
            best_score,
        )

    return (
        None,
        best_score,
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
        extra_used_titles: list[str] | None = None,
        trigger: str | None = None,
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
                f"{now_local.year} "
                "актуальная практика "
                "применение типичные ошибки "
                "проверка документы требования "
                "рекомендации"
            )

            with usage_context(
                "search_subtopic",
                metadata={
                    "topic": topic_title,
                    "trigger": trigger,
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

            # Учитываем ещё и последние публикации
            # всего канала независимо от parent-topic.
            #
            # Например, после статьи про изменения
            # "с 1 сентября" по одной теме следующая
            # статья по другой теме не должна
            # автоматически строиться вокруг той же даты.
            recent_titles = (
                await db.list_recent_article_titles(
                    limit=15,
                )
            )

            comparison_titles = list(
                dict.fromkeys(
                    [
                        *recent_titles,
                        *(extra_used_titles or []),
                    ]
                )
            )

            used_for_gpt = list(
                dict.fromkeys(
                    [
                        *used,
                        *comparison_titles,
                    ]
                )
            )

            # ----------------------------------------
            # ОДИН GPT-ВЫЗОВ -> НЕСКОЛЬКО КАНДИДАТОВ
            #
            # Раньше при отклонении одного варианта
            # запускался новый GPT-запрос.
            #
            # Теперь модель сразу предлагает несколько
            # разных вариантов, а локальный код бесплатно
            # выбирает первый подходящий.
            # ----------------------------------------

            with usage_context(
                "subtopic_select",
                metadata={
                    "topic": topic_title,
                    "candidate_count": 6,
                    "trigger": trigger,
                },
            ):
                candidates = (
                    await self.gpt.select_article_subtopic(
                        topic=topic_title,
                        sources=sources,
                        used_subtopics=used_for_gpt,
                        candidate_count=6,
                    )
                )

            if isinstance(
                candidates,
                str,
            ):
                candidates = [
                    candidates
                ]

            for candidate_index, subtopic in enumerate(
                candidates,
                1,
            ):

                subtopic = " ".join(
                    str(subtopic or "").split()
                )

                if not subtopic:
                    continue

                if (
                    subtopic.casefold()
                    in {
                        "__no_relevant_subtopic__",
                        "no_relevant_subtopic",
                    }
                ):
                    continue

                if (
                    subtopic.casefold()
                    == topic_title.casefold()
                ):
                    continue

                if (
                    looks_like_news_first_subtopic(
                        subtopic
                    )
                    and not topic_explicitly_requests_news(
                        topic_title
                    )
                ):
                    log.info(
                        "Плановая news-first подтема "
                        "отклонена локально: "
                        "parent=%r candidate=%r index=%s",
                        topic_title,
                        subtopic,
                        candidate_index,
                    )

                    continue

                similar_title, similarity = (
                    _find_similar_global_article_title(
                        subtopic,
                        comparison_titles,
                        threshold=0.58,
                    )
                )

                if similar_title is not None:
                    log.warning(
                        "Global similar subtopic rejected "
                        "locally: parent=%r index=%s "
                        "score=%.3f candidate=%r "
                        "previous=%r",
                        topic_title,
                        candidate_index,
                        similarity,
                        subtopic,
                        similar_title,
                    )

                    continue

                log.info(
                    "Плановая подтема выбрана "
                    "из одного GPT-пакета: "
                    "parent=%s subtopic=%s "
                    "similarity=%.3f index=%s "
                    "candidates=%s",
                    topic_title,
                    subtopic,
                    similarity,
                    candidate_index,
                    len(candidates),
                )

                return subtopic

            log.warning(
                "Все подтемы одного GPT-пакета "
                "отклонены локальными проверками: "
                "parent=%r candidates=%s",
                topic_title,
                len(candidates),
            )

            return None

        except Exception:
            log.exception(
                "Не удалось выбрать плановую "
                "подтему для %s. "
                "Использую исходную тему.",
                topic_title,
            )

            return None


    async def _generate_long_candidate(
        self,
        topic_title: str,
        subtopic: str | None = None,
        trigger: str | None = None,
    ) -> tuple[str, str]:
        """
        Генерирует только LONG.

        Важно:
        SHORT и изображение здесь намеренно
        НЕ создаются. Сначала LONG должен пройти
        проверку на смысловой дубль.
        """
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
            "действующие требования "
            "актуальная практика "
            "применение типичные ошибки "
            "проверка документы рекомендации"
        )

        with usage_context(
            "search_article",
            metadata={
                "topic": topic_title,
                "subtopic": subtopic,
                "trigger": trigger,
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

        article_system_prompt = (
            custom_article_prompt
            if custom_article_prompt
            else ARTICLE_SYSTEM_PROMPT
        )

        log.info(
            "Генерация LONG: prompt=%s (%s chars)",
            (
                "CUSTOM"
                if custom_article_prompt
                else "DEFAULT"
            ),
            len(article_system_prompt),
        )

        with usage_context(
            "article_full",
            metadata={
                "topic": topic_title,
                "subtopic": subtopic,
                "trigger": trigger,
            },
        ):
            title, full_body = (
                await self.gpt
                .generate_article_from_sources(
                    topic=topic_title,
                    sources=sources,
                    subtopic=subtopic,
                    max_chars=3200,
                    system_prompt=article_system_prompt,
                )
            )

        title = clean_article_text(
            title
        )

        if custom_article_prompt:
            full_body = (
                clean_short_article_text(
                    full_body
                )
            )
        else:
            full_body = clean_article_text(
                full_body
            )

        if not custom_article_prompt:
            full_body = enforce_single_blog_link(
                full_body
            )

        log.info(
            "LONG готов: title=%r chars=%s",
            title,
            len(full_body),
        )

        return (
            title,
            full_body,
        )


    async def _complete_generated_article(
        self,
        topic_title: str,
        subtopic: str | None,
        title: str,
        full_body: str,
        trigger: str | None = None,
    ) -> tuple[
        str,
        str,
        bytes,
    ]:
        """
        Создаёт SHORT + изображение только после того,
        как LONG уже признан пригодным к публикации.
        """
        focus_topic = (
            subtopic or topic_title
        ).strip()

        custom_short_prompt = (
            await db.get_setting(
                "prompt_short_system",
                "",
            )
        ).strip()

        short_system_prompt = (
            custom_short_prompt
            if custom_short_prompt
            else SYNCBOT_SYSTEM_PROMPT
        )

        image_prompt_template = (
            await db.get_setting(
                "prompt_image_template",
                "",
            )
        ).strip() or DEFAULT_IMAGE_PROMPT_TEMPLATE

        with usage_context(
            "article_short",
            metadata={
                "topic": topic_title,
                "subtopic": subtopic,
                "trigger": trigger,
            },
        ):
            short_title, short_body = (
                await self.gpt
                .generate_syncbot_article_from_article(
                    topic=topic_title,
                    article_title=title,
                    article_body=full_body,
                    max_chars=820,
                    system_prompt=short_system_prompt,
                )
            )

        short_title = clean_article_text(
            short_title
        ) or title

        short_body = clean_short_article_text(
            short_body
        )

        if not custom_short_prompt:
            short_body = enforce_single_blog_link(
                short_body
            )

        log.info(
            "SHORT готов: chars=%s",
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
                    "YandexART: генерация изображения, "
                    "попытка %s/2",
                    attempt,
                )

                with usage_context(
                    "image_generation",
                    metadata={
                        "topic": topic_title,
                        "subtopic": subtopic,
                        "trigger": trigger,
                    },
                ):
                    image_bytes = (
                        await self.art.generate_image(
                            prompt
                        )
                    )

                if not image_bytes:
                    raise RuntimeError(
                        "YandexART вернул пустое изображение"
                    )

                if len(image_bytes) < 10_000:
                    raise RuntimeError(
                        "YandexART вернул слишком "
                        "маленький файл: "
                        f"{len(image_bytes)} bytes"
                    )

                log.info(
                    "YandexART: изображение готово, "
                    "size=%s bytes",
                    len(image_bytes),
                )

                break

            except Exception as exc:
                last_error = exc

                log.exception(
                    "YandexART: ошибка генерации, "
                    "попытка %s/2",
                    attempt,
                )

                if attempt < 2:
                    await asyncio.sleep(
                        3
                    )

        if not image_bytes:
            raise RuntimeError(
                "Не удалось получить изображение "
                "YandexART после 2 попыток. "
                f"Последняя ошибка: {last_error}"
            )

        return (
            short_title,
            short_body,
            image_bytes,
        )


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
        """
        Полный wrapper для старых вызовов.

        Поведение не меняется:
        LONG + SHORT + IMAGE.
        """

        title, full_body = (
            await self._generate_long_candidate(
                topic_title,
                subtopic=subtopic,
            )
        )

        (
            short_title,
            short_body,
            image_bytes,
        ) = await self._complete_generated_article(
            topic_title,
            subtopic,
            title,
            full_body,
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
        result: dict[str, Any] = {
            "publication_id": publication_id,
            "telegram": "pending",
        }

        try:
            if not image_bytes:
                raise RuntimeError(
                    "Нет изображения: публикация отменена"
                )

            # -------------------------------------------------
            # BOT API SEED FOR DZEN
            # -------------------------------------------------
            #
            # Dzen стабильно импортирует обычный send_photo.
            # Telegram Web long-post больше не используем.
            #
            # В seed кладём короткий законченный текст.
            # Если последующее редактирование Dzen не сработает,
            # в статье не останется обрезанный LONG.
            # -------------------------------------------------

            short_body_clean = (
                clean_short_article_text(
                    short_body
                )
            )

            public_caption = (
                build_public_short_caption_html(
                    title,
                    short_body_clean,
                )
            )

            log.info(
                "Bot API: публикую финальный "
                "Telegram SHORT, chars=%s",
                len(public_caption),
            )

            public_message = None

            # -----------------------------------------
            # SAFE TELEGRAM CONNECT RETRY
            #
            # Повторяем только ошибки, при которых
            # соединение с Telegram точно НЕ было
            # установлено:
            #
            # - DNS failure;
            # - Cannot connect to host;
            # - Connect call failed.
            #
            # Обычный timeout после начала HTTP-запроса
            # автоматически НЕ повторяем, чтобы не
            # создать дубль публикации.
            # -----------------------------------------

            telegram_connect_markers = (
                "clientconnectorerror",
                "cannot connect to host",
                "connect call failed",
                "temporary failure in name resolution",
                "name or service not known",
                "nodename nor servname provided",
            )

            for send_attempt in range(
                1,
                4,
            ):
                try:
                    public_message = (
                        await self.bot.send_photo(
                            chat_id=(
                                self.cfg.telegram_channel_id
                            ),
                            photo=BufferedInputFile(
                                image_bytes,
                                filename="article.jpg",
                            ),
                            caption=public_caption,
                            parse_mode="HTML",
                            disable_notification=True,
                            request_timeout=180,
                        )
                    )

                    break

                except TelegramNetworkError as exc:
                    error_text = str(
                        exc
                    ).casefold()

                    safe_connect_error = any(
                        marker in error_text
                        for marker in (
                            telegram_connect_markers
                        )
                    )

                    if (
                        not safe_connect_error
                        or send_attempt >= 3
                    ):
                        raise

                    delay = (
                        3
                        if send_attempt == 1
                        else 8
                    )

                    log.warning(
                        "Telegram send_photo: "
                        "ошибка подключения, "
                        "безопасный retry %s/3 "
                        "через %s сек: %s",
                        send_attempt,
                        delay,
                        exc,
                    )

                    await asyncio.sleep(
                        delay
                    )

            if public_message is None:
                raise RuntimeError(
                    "Telegram send_photo не вернул "
                    "результат после retry"
                )

            public_message_id = (
                public_message.message_id
            )

            log.info(
                "Bot API: финальный Telegram SHORT "
                "опубликован, message_id=%s. "
                "Жду импорт в Dzen.",
                public_message_id,
            )

            await db.set_telegram_result(
                publication_id,
                "published",
                message_id=str(
                    public_message_id
                ),
            )

            async def replace_after_dzen_sync() -> None:
                dzen_ready = False
                dzen_article_href: str | None = None

                try:
                    dzen_profile_dir = str(
                        os.getenv(
                            "DZEN_COMMENT_PROFILE_DIR",
                            "",
                        )
                        or os.getenv(
                            "DZEN_PROFILE_DIR",
                            "",
                        )
                    ).strip()

                    dzen_studio_url = str(
                        os.getenv(
                            "DZEN_COMMENTS_URL",
                            "",
                        )
                    ).strip()

                    if (
                        dzen_profile_dir
                        and dzen_studio_url
                    ):
                        formatter = DzenRichFormatter(
                            headless=True
                        )

                        log.info(
                            "Dzen: жду импорт Telegram SHORT: %s",
                            title,
                        )

                        try:
                            dzen_article_href = (
                                await formatter.wait_for_article(
                                    profile_dir=dzen_profile_dir,
                                    studio_url=dzen_studio_url,
                                    article_title=title,
                                    timeout_seconds=180,
                                    poll_seconds=5,
                                )
                            )

                            dzen_ready = bool(
                                dzen_article_href
                            )
                        except Exception:
                            log.exception(
                                "Dzen: ошибка ожидания seed-статьи"
                            )

                        if dzen_ready:
                            log.info(
                                "Dzen: Telegram SHORT импортирован. "
                                "Финализирую статью "
                                "за одну editor-сессию."
                            )

                            try:
                                finalize_result = (
                                    await formatter
                                    .finalize_article(
                                        profile_dir=dzen_profile_dir,
                                        studio_url=dzen_studio_url,
                                        article_title=title,
                                        source_body=full_body,
                                        article_href=dzen_article_href,
                                        publish=True,
                                    )
                                )

                                log.info(
                                    "Dzen: finalize готов; "
                                    "published=%s "
                                    "body_replaced=%s "
                                    "formatted=%s "
                                    "old_chars=%s "
                                    "new_chars=%s "
                                    "mapped=%s "
                                    "applied=%s "
                                    "already_active=%s "
                                    "unsupported=%s "
                                    "image_preserved=%s",
                                    finalize_result.get(
                                        "published"
                                    ),
                                    finalize_result.get(
                                        "body_replaced"
                                    ),
                                    finalize_result.get(
                                        "formatted"
                                    ),
                                    finalize_result.get(
                                        "old_body_chars"
                                    ),
                                    finalize_result.get(
                                        "new_body_chars"
                                    ),
                                    finalize_result.get(
                                        "mapped_lines"
                                    ),
                                    len(
                                        finalize_result.get(
                                            "applied",
                                            [],
                                        )
                                    ),
                                    len(
                                        finalize_result.get(
                                            "already_active",
                                            [],
                                        )
                                    ),
                                    len(
                                        finalize_result.get(
                                            "unsupported",
                                            [],
                                        )
                                    ),
                                    finalize_result.get(
                                        "image_preserved"
                                    ),
                                )

                            except Exception:
                                log.exception(
                                    "Dzen: finalize не удался. "
                                    "Seed остаётся законченной "
                                    "короткой статьёй."
                                )

                        else:
                            log.warning(
                                "Dzen: seed-статья не появилась "
                                "за 180 секунд."
                            )

                    else:
                        log.warning(
                            "Dzen: настройки не заданы; "
                            "обработка Dzen пропущена."
                        )

                finally:
                    # Telegram SHORT уже является
                    # окончательной публикацией.
                    #
                    # НИЧЕГО не удаляем и повторно
                    # в Telegram не публикуем.
                    log.info(
                        "Готово: Dzen ready=%s; "
                        "Telegram SHORT сохранён, "
                        "message_id=%s.",
                        dzen_ready,
                        public_message_id,
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
            ] = False

            result[
                "dzen_finalize_scheduled"
            ] = True

            result[
                "telegram_long_format"
            ] = "bot_api_final_short_dzen_transport"

            result[
                "telegram_short_format"
            ] = "photo_caption_html"

        except Exception as exc:
            log.exception(
                "Ошибка публикации Dzen seed "
                "через Bot API"
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

            priority = int(
                topic.get(
                    "priority",
                    0,
                )
                or 0
            )

            # Автоподбор подтемы на ПЕРВОЙ попытке
            # сохраняет прежнюю логику:
            #
            # auto + обычная тема -> да
            # urgent_random      -> нет
            # priority           -> нет
            discover_subtopic_initially = (
                trigger == "auto"
                and priority == 0
            )

            # Но защита от смысловых дублей нужна
            # для ЛЮБОЙ случайной публикации.
            #
            # Иначе кнопка urgent_random может
            # публиковать почти одинаковые статьи
            # подряд, обходя весь story-guard.
            duplicate_guard_enabled = (
                trigger in {
                    "auto",
                    "urgent_random",
                }
            )

            recent_articles_for_final = (
                await db.list_recent_articles(
                    limit=15,
                )
                if duplicate_guard_enabled
                else []
            )

            recent_titles_for_final = [
                article.get(
                    "article_title",
                    ""
                )
                for article
                in recent_articles_for_final
                if article.get(
                    "article_title"
                )
            ]

            rejected_generation_titles: list[str] = []
            generation_result = None

            # Обычная статья получает максимум
            # две полноценные попытки.
            #
            # Если первая статья оказалась смысловым
            # дублем, второй раз выбирается другой
            # акцент с учётом отклонённого результата.
            generation_attempts = (
                2
                if duplicate_guard_enabled
                else 1
            )

            for generation_attempt in range(
                1,
                generation_attempts + 1,
            ):

                discover_subtopic_now = (
                    discover_subtopic_initially
                    or (
                        duplicate_guard_enabled
                        and generation_attempt > 1
                    )
                )

                if discover_subtopic_now:
                    selected_subtopic = (
                        await self._select_auto_subtopic(
                            topic_id,
                            topic_title,
                            extra_used_titles=(
                                rejected_generation_titles
                            ),
                            trigger=trigger,
                        )
                    )
                else:
                    selected_subtopic = None


                # =====================================
                # 1. СНАЧАЛА ТОЛЬКО LONG
                # =====================================

                try:
                    (
                        title,
                        full_body,
                    ) = await self._generate_long_candidate(
                        topic_title,
                        subtopic=selected_subtopic,
                        trigger=trigger,
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
                        "Ошибка генерации LONG"
                    )

                    return {
                        "status": "generation_error",
                        "topic": topic_title,
                        "error": str(exc),
                    }


                # =====================================
                # 2. ПРОВЕРКА ДУБЛЯ ДО SHORT И IMAGE
                # =====================================

                if duplicate_guard_enabled:

                    similar_article, similarity = (
                        _find_similar_global_article_story(
                            title,
                            full_body,
                            recent_articles_for_final,
                            threshold=0.66,
                        )
                    )

                    if similar_article is not None:

                        similar_title = (
                            similar_article.get(
                                "article_title",
                                "",
                            )
                        )

                        similar_lead = (
                            _first_article_paragraph(
                                similar_article.get(
                                    "article_body",
                                    "",
                                )
                            )
                        )

                        log.warning(
                            "LONG story rejected BEFORE "
                            "SHORT/IMAGE: "
                            "attempt=%s score=%.3f "
                            "title=%r previous=%r",
                            generation_attempt,
                            similarity,
                            title,
                            similar_title,
                        )

                        rejected_generation_titles.append(
                            "НЕ ПОВТОРЯТЬ СЮЖЕТ: "
                            + similar_title
                            + " | "
                            + similar_lead[:350]
                        )

                        rejected_generation_titles.append(
                            title
                        )

                        if selected_subtopic:
                            rejected_generation_titles.append(
                                selected_subtopic
                            )

                        if (
                            generation_attempt
                            < generation_attempts
                        ):
                            log.info(
                                "LONG отклонён. "
                                "SHORT и изображение "
                                "НЕ генерировались. "
                                "Пробую другой акцент."
                            )

                            continue

                        await db.release_topic(
                            topic_id
                        )

                        return {
                            "status": "duplicate_rejected",
                            "topic": topic_title,
                            "article_title": title,
                            "similar_to": similar_title,
                            "similarity": similarity,
                        }

                    log.info(
                        "LONG story check OK: "
                        "title=%r similarity=%.3f",
                        title,
                        similarity,
                    )


                # =====================================
                # 3. ТОЛЬКО ПОСЛЕ ПРОВЕРКИ:
                #    SHORT + IMAGE
                # =====================================

                try:
                    (
                        short_title,
                        short_body,
                        image_bytes,
                    ) = await self._complete_generated_article(
                        topic_title,
                        selected_subtopic,
                        title,
                        full_body,
                        trigger=trigger,
                    )

                except Exception as exc:
                    await db.release_topic(
                        topic_id
                    )

                    log.exception(
                        "Ошибка генерации SHORT/IMAGE"
                    )

                    return {
                        "status": "generation_error",
                        "topic": topic_title,
                        "error": str(exc),
                    }


                generation_result = (
                    title,
                    full_body,
                    short_title,
                    short_body,
                    image_bytes,
                )

                break


            if generation_result is None:
                await db.release_topic(
                    topic_id
                )

                return {
                    "status": "generation_error",
                    "topic": topic_title,
                    "error": (
                        "Не удалось получить "
                        "уникальную статью"
                    ),
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
