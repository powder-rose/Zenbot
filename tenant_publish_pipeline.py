from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

import tenant_db
from article_service import build_short_rich_message
from dzen_rich_formatter import (
    DzenRichFormatter,
    parse_dzen_markup,
)


log = logging.getLogger(__name__)


class TenantPublishPipeline:
    """
    Универсальный SaaS pipeline публикации.

    Telegram:
        Bot API only.

    Если Dzen пользователя подключён:
        1. отправляем обычный photo + caption;
        2. ждём импорт статьи в Dzen;
        3. заменяем импортированный body на полный LONG;
        4. применяем rich-форматирование;
        5. удаляем временный Telegram media-post;
        6. публикуем SHORT Rich Message.

    Если Dzen не подключён:
        сразу публикуем SHORT.

    Ошибка Dzen никогда не должна блокировать
    итоговую Telegram-публикацию.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        headless: bool = True,
        dzen_wait_timeout_seconds: int = 180,
        dzen_poll_seconds: int = 5,
    ) -> None:
        self.bot = bot

        self.headless = bool(
            headless
        )

        self.dzen_wait_timeout_seconds = max(
            1,
            int(
                dzen_wait_timeout_seconds
            ),
        )

        self.dzen_poll_seconds = max(
            1,
            int(
                dzen_poll_seconds
            ),
        )


    async def _get_dzen_config(
        self,
        user_id: int,
    ) -> dict[str, str] | None:
        """
        Возвращает конфигурацию только для реально
        включённого Dzen-аккаунта.

        Отсутствие Dzen — нормальное состояние SaaS.
        """

        row = await tenant_db.tenant_dzen_account(
            user_id
        )

        if row is None:
            return None

        if not bool(
            row["enabled"]
        ):
            return None

        studio_url = str(
            row["comments_url"] or ""
        ).strip()

        profile_dir = str(
            row["profile_dir"] or ""
        ).strip()

        if (
            not studio_url
            or not profile_dir
        ):
            log.warning(
                "Tenant Dzen config incomplete: "
                "user=%s",
                user_id,
            )

            return None

        if not Path(
            profile_dir
        ).exists():
            log.warning(
                "Tenant Dzen profile missing: "
                "user=%s profile=%s",
                user_id,
                profile_dir,
            )

            return None

        return {
            "studio_url": studio_url,
            "profile_dir": profile_dir,
        }


    def _build_seed_caption(
        self,
        *,
        title: str,
        full_body: str,
    ) -> str:
        """
        Обычный Telegram photo caption нужен только
        как транспорт для импорта в Dzen.

        Используем начало настоящей LONG-статьи,
        а не технический placeholder.

        Важно:
        Bot API photo caption <= 1024 символов.
        """

        clean_title = str(
            title or ""
        ).strip()

        if not clean_title:
            raise RuntimeError(
                "Tenant Dzen seed: "
                "пустой заголовок"
            )

        # Заголовок должен попасть в Dzen полностью,
        # иначе wait_for_article() не сможет
        # однозначно найти статью.
        reserved = len(
            clean_title
        ) + 2

        if reserved >= 1024:
            raise RuntimeError(
                "Tenant Dzen seed: "
                "заголовок слишком длинный "
                "для photo caption"
            )

        parsed = parse_dzen_markup(
            full_body
        )

        plain_body = "\n".join(
            parsed.lines
        ).strip()

        available = (
            1024
            - reserved
        )

        seed_body = plain_body[
            :available
        ]

        # Не обрываем последнее слово,
        # если это можно сделать без слишком
        # большой потери текста.
        if (
            len(plain_body)
            > available
        ):
            cut = max(
                seed_body.rfind("\n"),
                seed_body.rfind(" "),
            )

            if cut >= max(
                100,
                available - 150,
            ):
                seed_body = (
                    seed_body[:cut]
                    .rstrip()
                )

        caption = (
            clean_title
            if not seed_body
            else (
                clean_title
                + "\n\n"
                + seed_body
            )
        )

        if len(caption) > 1024:
            raise RuntimeError(
                "Tenant Dzen seed: "
                "caption превысил 1024 символа"
            )

        return caption


    def _build_public_short_caption_html(
        self,
        *,
        title: str,
        short_body: str,
    ) -> str:
        """
        Финальный Telegram caption с сохранением
        пользовательского rich-formatting.

        Один и тот же photo-post остаётся пригодным
        для Dzen import: Telegram передаёт в канал
        обычный видимый текст + entities.
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

        selected: list[
            tuple[int, str]
        ] = []

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

        spans_by_line: dict[
            int,
            list,
        ] = {}

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
                line_index
                in parsed.blockquotes
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


    async def _send_public_short(
        self,
        *,
        chat_id: int,
        title: str,
        short_body: str,
        image_bytes: bytes,
    ) -> int:
        """
        Один и тот же Telegram photo-post выполняет
        сразу две функции:

        1. это финальный SHORT для подписчиков;
        2. это transport-публикация для импорта в Dzen.

        Пост после синхронизации НЕ удаляется.
        """

        caption = (
            self._build_public_short_caption_html(
                title=title,
                short_body=short_body,
            )
        )

        image = BufferedInputFile(
            image_bytes,
            filename="article.jpg",
        )

        message = await self.bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=caption,
            parse_mode="HTML",
            disable_notification=True,
            request_timeout=180,
        )

        log.info(
            "Tenant public SHORT sent: "
            "channel=%s message=%s "
            "caption_chars=%s "
            "dzen_transport=True",
            chat_id,
            message.message_id,
            len(caption),
        )

        return int(
            message.message_id
        )


    async def _delete_seed(
        self,
        *,
        chat_id: int,
        message_id: int,
    ) -> bool:
        try:
            await self.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

            log.info(
                "Tenant Dzen seed deleted: "
                "channel=%s message=%s",
                chat_id,
                message_id,
            )

            return True

        except Exception:
            log.exception(
                "Tenant Dzen seed delete failed: "
                "channel=%s message=%s",
                chat_id,
                message_id,
            )

            return False


    async def _send_short(
        self,
        *,
        chat_id: int,
        title: str,
        short_body: str,
        image_bytes: bytes,
    ) -> int:
        """
        Сохраняем существующее поведение tenant_service:
        сначала Rich Message, затем безопасный fallback.
        """

        try:
            rich = build_short_rich_message(
                title,
                short_body,
                image_bytes,
            )

            message = await self.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=rich,
            )

            return int(
                message.message_id
            )

        except Exception as rich_exc:
            log.warning(
                "Tenant SHORT Rich Message "
                "недоступен channel=%s: %s. "
                "Использую fallback.",
                chat_id,
                rich_exc,
            )

        image = BufferedInputFile(
            image_bytes,
            filename="article.jpg",
        )

        photo = await self.bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=(
                str(title or "")[:1024]
                or None
            ),
        )

        text = str(
            short_body or ""
        ).strip()

        while text:
            chunk = text[:4000]

            if (
                len(text) > 4000
                and "\n" in chunk
            ):
                cut = chunk.rfind(
                    "\n"
                )

                if cut > 2500:
                    chunk = chunk[:cut]

            await self.bot.send_message(
                chat_id=chat_id,
                text=chunk,
            )

            text = text[
                len(chunk):
            ].lstrip()

        return int(
            photo.message_id
        )


    async def _publish_to_dzen(
        self,
        *,
        user_id: int,
        chat_id: int,
        title: str,
        full_body: str,
        dzen: dict[str, str],
    ) -> dict[str, Any]:
        """
        Dzen-ветка для уже опубликованного SHORT.

        Telegram photo-post уже является финальной
        публикацией пользователя. Dzen импортирует
        его как исходник, после чего мы заменяем
        body на полный LONG и применяем форматирование.

        Telegram-пост никогда не удаляем.
        """

        result: dict[str, Any] = {
            "attempted": True,
            "synced": False,
            "body_replaced": False,
            "formatted": False,
            "seed_deleted": False,
            "telegram_post_preserved": True,
            "error": None,
        }

        try:
            formatter = DzenRichFormatter(
                headless=self.headless
            )

            article_href = await formatter.wait_for_article(
                profile_dir=dzen[
                    "profile_dir"
                ],
                studio_url=dzen[
                    "studio_url"
                ],
                article_title=title,
                timeout_seconds=(
                    self.dzen_wait_timeout_seconds
                ),
                poll_seconds=(
                    self.dzen_poll_seconds
                ),
            )

            result[
                "synced"
            ] = bool(
                article_href
            )

            result[
                "article_href"
            ] = article_href

            if not article_href:
                raise RuntimeError(
                    "Статья не появилась "
                    "в Dzen за время ожидания"
                )

            # ----------------------------------------
            # ONE DZEN EDITOR SESSION
            # ----------------------------------------
            #
            # Раньше SaaS делал:
            #
            # replace_article_body()
            # → save
            # → новый Chromium/editor
            # → format_article()
            # → save
            #
            # Теперь весь finalize выполняется
            # за одну editor-сессию и один save.
            # ----------------------------------------

            finalize_result = (
                await formatter.finalize_article(
                    profile_dir=dzen[
                        "profile_dir"
                    ],
                    studio_url=dzen[
                        "studio_url"
                    ],
                    article_title=title,
                    source_body=full_body,
                    article_href=article_href,
                    publish=True,
                )
            )

            result[
                "body_replaced"
            ] = bool(
                finalize_result.get(
                    "body_replaced"
                )
            )

            result[
                "formatted"
            ] = bool(
                finalize_result.get(
                    "formatted"
                )
            )

            result[
                "finalize_result"
            ] = finalize_result

            # Сохраняем совместимость с диагностикой
            # старого pipeline. Внешний код пока может
            # читать эти поля.
            result[
                "replace_result"
            ] = {
                "published": (
                    finalize_result.get(
                        "published"
                    )
                ),
                "old_body_chars": (
                    finalize_result.get(
                        "old_body_chars"
                    )
                ),
                "new_body_chars": (
                    finalize_result.get(
                        "new_body_chars"
                    )
                ),
                "mapped_lines": (
                    finalize_result.get(
                        "mapped_lines"
                    )
                ),
                "image_preserved": (
                    finalize_result.get(
                        "image_preserved"
                    )
                ),
            }

            result[
                "format_result"
            ] = {
                "published": (
                    finalize_result.get(
                        "published"
                    )
                ),
                "applied": (
                    finalize_result.get(
                        "applied",
                        [],
                    )
                ),
                "already_active": (
                    finalize_result.get(
                        "already_active",
                        [],
                    )
                ),
                "unsupported": (
                    finalize_result.get(
                        "unsupported",
                        [],
                    )
                ),
            }

            log.info(
                "Tenant Dzen finalize complete: "
                "user=%s channel=%s "
                "published=%s replaced=%s "
                "formatted=%s image_preserved=%s",
                user_id,
                chat_id,
                finalize_result.get(
                    "published"
                ),
                result.get(
                    "body_replaced"
                ),
                result.get(
                    "formatted"
                ),
                finalize_result.get(
                    "image_preserved"
                ),
            )

            log.info(
                "Tenant Dzen publication ready: "
                "user=%s channel=%s title=%r",
                user_id,
                chat_id,
                title,
            )

        except Exception as exc:
            result[
                "error"
            ] = str(
                exc
            )

            log.exception(
                "Tenant Dzen pipeline failed: "
                "user=%s channel=%s title=%r",
                user_id,
                chat_id,
                title,
            )

        return result


    async def publish_one(
        self,
        *,
        user_id: int,
        chat_id: int,
        title: str,
        full_body: str,
        short_body: str,
        image_bytes: bytes,
    ) -> dict[str, Any]:
        """
        Полный pipeline одного tenant-канала.

        Если Dzen подключён:
            1. сразу публикуем финальный Telegram
               photo + SHORT;
            2. этот же пост импортируется в Dzen;
            3. в Dzen заменяем SHORT на LONG;
            4. применяем rich-format;
            5. Telegram-пост оставляем как есть.

        Если Dzen не подключён:
            сохраняем прежний Rich Message SHORT.
        """

        result: dict[str, Any] = {
            "chat_id": int(
                chat_id
            ),
            "short_message_id": None,
            "dzen": {
                "attempted": False,
                "synced": False,
                "body_replaced": False,
                "formatted": False,
                "seed_deleted": False,
                "telegram_post_preserved": False,
                "error": None,
            },
        }

        dzen = await self._get_dzen_config(
            user_id
        )

        # ------------------------------------------
        # DZEN CONNECTED
        #
        # Один Telegram post = финальный SHORT
        # + transport для Dzen.
        # ------------------------------------------

        if dzen is not None:
            short_message_id = (
                await self._send_public_short(
                    chat_id=chat_id,
                    title=title,
                    short_body=(
                        short_body.strip()
                        or full_body
                    ),
                    image_bytes=image_bytes,
                )
            )

            result[
                "short_message_id"
            ] = short_message_id

            result[
                "dzen"
            ] = await self._publish_to_dzen(
                user_id=user_id,
                chat_id=chat_id,
                title=title,
                full_body=full_body,
                dzen=dzen,
            )

        # ------------------------------------------
        # NO DZEN
        #
        # Оставляем прежний Rich Message.
        # ------------------------------------------

        else:
            short_message_id = (
                await self._send_short(
                    chat_id=chat_id,
                    title=title,
                    short_body=short_body,
                    image_bytes=image_bytes,
                )
            )

            result[
                "short_message_id"
            ] = short_message_id

        log.info(
            "Tenant publish complete: "
            "user=%s channel=%s "
            "short_message=%s "
            "dzen_attempted=%s "
            "dzen_synced=%s "
            "dzen_formatted=%s "
            "telegram_preserved=%s",
            user_id,
            chat_id,
            result["short_message_id"],
            result["dzen"].get(
                "attempted"
            ),
            result["dzen"].get(
                "synced"
            ),
            result["dzen"].get(
                "formatted"
            ),
            result["dzen"].get(
                "telegram_post_preserved"
            ),
        )

        return result
