from __future__ import annotations

import asyncio

from dataclasses import dataclass
import re


@dataclass(slots=True)
class DzenFormatSpan:
    line: int
    start: int
    end: int
    style: str
    text: str


@dataclass(slots=True)
class DzenParsedMarkup:
    lines: list[str]
    spans: list[DzenFormatSpan]
    blockquotes: set[int]


_EXPLICIT_OPEN = {
    "[[B]]": "bold",
    "[[I]]": "italic",
    "[[U]]": "underline",
    "[[S]]": "strike",
}

_EXPLICIT_CLOSE = {
    "[[/B]]": "bold",
    "[[/I]]": "italic",
    "[[/U]]": "underline",
    "[[/S]]": "strike",
}

_HTML_OPEN = {
    "<b>": "bold",
    "<i>": "italic",
    "<u>": "underline",
    "<s>": "strike",
}

_HTML_CLOSE = {
    "</b>": "bold",
    "</i>": "italic",
    "</u>": "underline",
    "</s>": "strike",
}


_INLINE_MARKERS = {
    "==": "bold",
    "**": "bold",
    "__": "underline",
    "~~": "strike",
    "_": "italic",
    "*": "italic",
}


def _compress_spans(
    *,
    line_index: int,
    text: str,
    styles_by_char: list[set[str]],
) -> list[DzenFormatSpan]:
    result: list[DzenFormatSpan] = []

    for style in (
        "bold",
        "italic",
        "underline",
        "strike",
    ):
        start = None

        for index in range(len(text) + 1):
            enabled = (
                index < len(text)
                and style in styles_by_char[index]
            )

            if enabled and start is None:
                start = index

            if not enabled and start is not None:
                if index > start:
                    result.append(
                        DzenFormatSpan(
                            line=line_index,
                            start=start,
                            end=index,
                            style=style,
                            text=text[start:index],
                        )
                    )

                start = None

    return result


def parse_dzen_markup(
    source: str,
) -> DzenParsedMarkup:
    source = str(source or "").replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    )

    source_lines = source.split("\n")

    plain_lines: list[str] = []
    spans: list[DzenFormatSpan] = []
    blockquotes: set[int] = set()

    # [[B]] / [[I]] и т. п. могут при желании
    # охватывать несколько строк.
    explicit_active: set[str] = set()
    explicit_quote = False

    for line_index, original_line in enumerate(
        source_lines
    ):
        line = original_line

        leading_quote = False

        # Если [[Q]] открылся на предыдущей строке,
        # текущая строка тоже является цитатой.
        line_explicit_quote = bool(
            explicit_quote
        )

        match = re.match(
            r"^\s*>\s?",
            line,
        )

        if match:
            leading_quote = True
            line = line[match.end():]

        output: list[str] = []
        char_styles: list[set[str]] = []

        inline_active: set[str] = set()

        i = 0

        while i < len(line):
            matched = False

            # -----------------------------
            # HTML rich-теги.
            #
            # clean_short_article_text()
            # преобразует [[B]] / [[I]] / ...
            # именно в такой вид.
            # -----------------------------

            for token, style in _HTML_OPEN.items():
                if (
                    line[
                        i:i + len(token)
                    ].lower()
                    == token
                ):
                    explicit_active.add(
                        style
                    )
                    i += len(token)
                    matched = True
                    break

            if matched:
                continue

            for token, style in _HTML_CLOSE.items():
                if (
                    line[
                        i:i + len(token)
                    ].lower()
                    == token
                ):
                    explicit_active.discard(
                        style
                    )
                    i += len(token)
                    matched = True
                    break

            if matched:
                continue

            # -----------------------------
            # Явные SaaS-теги
            # -----------------------------

            if line.startswith(
                "[[Q]]",
                i,
            ):
                line_explicit_quote = True
                explicit_quote = True
                i += len("[[Q]]")
                continue

            if line.startswith(
                "[[/Q]]",
                i,
            ):
                explicit_quote = False
                i += len("[[/Q]]")
                continue

            for token, style in _EXPLICIT_OPEN.items():
                if line.startswith(
                    token,
                    i,
                ):
                    explicit_active.add(
                        style
                    )
                    i += len(token)
                    matched = True
                    break

            if matched:
                continue

            for token, style in _EXPLICIT_CLOSE.items():
                if line.startswith(
                    token,
                    i,
                ):
                    explicit_active.discard(
                        style
                    )
                    i += len(token)
                    matched = True
                    break

            if matched:
                continue

            # -----------------------------
            # Markdown-подобная разметка
            # -----------------------------

            for token in (
                "==",
                "**",
                "__",
                "~~",
                "_",
                "*",
            ):
                if not line.startswith(
                    token,
                    i,
                ):
                    continue

                style = _INLINE_MARKERS[
                    token
                ]

                # Маркер считаем форматированием,
                # только если он уже открыт либо
                # впереди есть закрывающая пара.
                has_pair = (
                    style in inline_active
                    or line.find(
                        token,
                        i + len(token),
                    ) != -1
                )

                if not has_pair:
                    continue

                if style in inline_active:
                    inline_active.remove(
                        style
                    )
                else:
                    inline_active.add(
                        style
                    )

                i += len(token)
                matched = True
                break

            if matched:
                continue

            output.append(
                line[i]
            )

            char_styles.append(
                set(explicit_active)
                | set(inline_active)
            )

            i += 1

        plain = "".join(
            output
        )

        plain_lines.append(
            plain
        )

        spans.extend(
            _compress_spans(
                line_index=line_index,
                text=plain,
                styles_by_char=char_styles,
            )
        )

        if (
            leading_quote
            or line_explicit_quote
        ) and plain.strip():
            blockquotes.add(
                line_index
            )

        # Обычный Markdown не переносим
        # автоматически через следующую строку.
        inline_active.clear()

    return DzenParsedMarkup(
        lines=plain_lines,
        spans=spans,
        blockquotes=blockquotes,
    )


# ============================================================
# DZEN RUNTIME
# ============================================================

import logging
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    Page,
    async_playwright,
)

from dzen_browser_lock import (
    DZEN_BROWSER_LOCK,
)


log = logging.getLogger(
    __name__
)


_DZEN_TOOLBAR_LABELS = {
    "bold": "Bold",
    "italic": "Italic",
    "strike": "Strike",
}


def _normalize_dzen_text(
    value: str,
) -> str:
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .split()
    )


class DzenRichFormatter:
    """
    Универсальный форматтер статьи Дзена.

    Не знает:
    - имени автора;
    - slug канала;
    - названия компании;
    - структуры конкретного prompt.

    Получает только:
    - profile_dir;
    - URL любой страницы Dzen Studio;
    - article_title;
    - исходный текст с rich-разметкой.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
    ) -> None:
        self.headless = bool(
            headless
        )

    async def _goto_publications(
        self,
        page: Page,
        start_url: str,
    ) -> None:
        await page.goto(
            start_url,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        await page.wait_for_timeout(
            2500
        )

        if (
            "/publications"
            in page.url
        ):
            return

        links = page.locator(
            "a[href]"
        )

        for index in range(
            await links.count()
        ):
            link = links.nth(
                index
            )

            href = (
                await link.get_attribute(
                    "href"
                )
                or ""
            ).strip()

            if not href:
                continue

            absolute = urljoin(
                page.url,
                href,
            )

            parsed = urlparse(
                absolute
            )

            path = (
                parsed.path
                or ""
            ).rstrip("/")

            # Нужна именно страница публикаций.
            #
            # /publications-stat не подходит.
            # Slug/id пользователя при этом
            # может быть любым.
            if not path.endswith(
                "/publications"
            ):
                continue

            await page.goto(
                absolute,
                wait_until="domcontentloaded",
                timeout=90000,
            )

            await page.wait_for_timeout(
                5000
            )

            return

        raise RuntimeError(
            "Dzen formatter: "
            "страница «Публикации» не найдена"
        )

    async def _find_article_link(
        self,
        page: Page,
        article_title: str,
        article_href: str | None = None,
    ):
        target = _normalize_dzen_text(
            article_title
        )

        target_href = str(
            article_href or ""
        ).strip()

        target_href_path = ""

        if target_href:
            target_href_path = (
                urlparse(
                    urljoin(
                        page.url,
                        target_href,
                    )
                )
                .path
                .rstrip("/")
            )

        # Dzen Studio может временно убрать только что
        # импортированную публикацию из списка во время
        # внутренней обработки. Поэтому после первого
        # появления не считаем короткое исчезновение ошибкой.
        max_attempts = 16

        for attempt in range(
            1,
            max_attempts + 1,
        ):
            links = page.locator(
                'a[href*="/a/"]'
            )

            count = await links.count()

            for index in range(
                count
            ):
                link = links.nth(
                    index
                )

                try:
                    current_href = str(
                        await link.get_attribute(
                            "href"
                        )
                        or ""
                    ).strip()

                    # Если статья уже была однажды
                    # идентифицирована, заголовок больше
                    # не используем как идентификатор.
                    if target_href:
                        current_href_path = (
                            urlparse(
                                urljoin(
                                    page.url,
                                    current_href,
                                )
                            )
                            .path
                            .rstrip("/")
                        )

                        if (
                            current_href_path
                            == target_href_path
                        ):
                            return link

                        continue

                    text = (
                        _normalize_dzen_text(
                            await link.inner_text()
                        )
                    )

                except Exception:
                    continue

                if (
                    text == target
                    or text.startswith(
                        target + " "
                    )
                ):
                    return link

            if attempt < max_attempts:
                log.info(
                    "Dzen formatter: "
                    "статья ещё не найдена, "
                    "попытка %s/%s: %s",
                    attempt,
                    max_attempts,
                    article_title,
                )

                await page.wait_for_timeout(
                    3000
                )

                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=90000,
                )

                await page.wait_for_timeout(
                    1500
                )

        raise RuntimeError(
            "Dzen formatter: "
            "не найдена статья: "
            + article_title
        )

    async def _open_editor(
        self,
        page: Page,
        article_title: str,
        article_href: str | None = None,
    ):
        article = (
            await self._find_article_link(
                page,
                article_title,
                article_href=article_href,
            )
        )

        row = article.locator(
            "xpath=ancestor::*["
            ".//button[@aria-label="
            "'Меню публикации']"
            "][1]"
        )

        if await row.count() == 0:
            raise RuntimeError(
                "Dzen formatter: "
                "не найдена строка публикации"
            )

        menu = row.locator(
            'button[aria-label="Меню публикации"]'
        ).first

        await menu.click(
            force=True
        )

        await page.wait_for_timeout(
            500
        )

        edit = page.get_by_text(
            "Отредактировать",
            exact=True,
        ).last

        if await edit.count() == 0:
            raise RuntimeError(
                "Dzen formatter: "
                "пункт «Отредактировать» "
                "не найден"
            )

        await edit.click(
            force=True
        )

        # ------------------------------------------
        # Dzen Studio — SPA. Иногда URL редактора
        # открывается быстро, а Draft.js появляется
        # только через несколько секунд.
        # ------------------------------------------

        async def close_help_popup():
            try:
                await page.keyboard.press(
                    "Escape"
                )

                await page.wait_for_timeout(
                    150
                )
            except Exception:
                pass

        async def wait_for_body(
            timeout_ms: int = 20000,
        ):
            elapsed = 0

            while elapsed < timeout_ms:
                editors = page.locator(
                    '[contenteditable="true"]'
                    '.public-DraftEditor-content'
                )

                count = await editors.count()

                if count >= 2:
                    body = editors.nth(1)

                    try:
                        if await body.is_visible():
                            blocks = body.locator(
                                '[data-block="true"]'
                            )

                            if (
                                await blocks.count()
                                > 0
                            ):
                                return body
                    except Exception:
                        pass

                await page.wait_for_timeout(
                    500
                )

                elapsed += 500

            return None

        await page.wait_for_timeout(
            1000
        )

        await close_help_popup()

        body = await wait_for_body(
            20000
        )

        if body is not None:
            log.info(
                "Dzen formatter: "
                "Draft.js редактор загружен"
            )

            return body

        # ------------------------------------------
        # Один безопасный reload.
        #
        # До этого момента никаких изменений статьи
        # ещё не выполнялось, поэтому unsaved state
        # потерять невозможно.
        # ------------------------------------------

        log.warning(
            "Dzen formatter: "
            "Draft.js не появился за 20 секунд; "
            "перезагружаю editor один раз"
        )

        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception:
            # Некоторые SPA страницы продолжают
            # фоновые запросы и формально могут
            # превысить timeout, хотя DOM уже готов.
            pass

        await page.wait_for_timeout(
            1500
        )

        await close_help_popup()

        body = await wait_for_body(
            20000
        )

        if body is not None:
            log.info(
                "Dzen formatter: "
                "Draft.js редактор загружен "
                "после reload"
            )

            return body

        editors = page.locator(
            '[contenteditable="true"]'
            '.public-DraftEditor-content'
        )

        raise RuntimeError(
            "Dzen formatter: "
            "тело Draft.js статьи не найдено "
            "после ожидания и reload; "
            f"editors={await editors.count()}, "
            f"url={page.url}"
        )

    async def _map_lines_to_blocks(
        self,
        body,
        parsed: DzenParsedMarkup,
    ) -> dict[int, int]:
        blocks = body.locator(
            '[data-block="true"]'
        )

        block_count = await blocks.count()

        dzen_lines: list[str] = []

        for index in range(
            block_count
        ):
            dzen_lines.append(
                _normalize_dzen_text(
                    await blocks.nth(
                        index
                    ).inner_text()
                )
            )

        mapping: dict[
            int,
            int,
        ] = {}

        search_from = 0

        for line_index, source_line in enumerate(
            parsed.lines
        ):
            wanted = _normalize_dzen_text(
                source_line
            )

            # Пустые строки для форматирования
            # нам не нужны.
            if not wanted:
                continue

            found = None

            for block_index in range(
                search_from,
                len(dzen_lines),
            ):
                if (
                    dzen_lines[
                        block_index
                    ]
                    == wanted
                ):
                    found = block_index
                    break

            if found is None:
                raise RuntimeError(
                    "Dzen formatter: "
                    "не удалось сопоставить строку "
                    f"{line_index}: "
                    f"{wanted[:120]!r}"
                )

            mapping[
                line_index
            ] = found

            search_from = (
                found + 1
            )

        return mapping

    async def _select_text(
        self,
        block,
        *,
        prefix: str,
        target: str,
    ) -> str:
        """
        Выделяет target внутри Draft.js блока.

        prefix передаём строкой, а длину
        вычисляет JavaScript. Поэтому emoji
        и UTF-16 offsets не ломают Range.
        """

        result = await block.evaluate(
            """
            (root, args) => {
                root.scrollIntoView({
                    block: 'center'
                });

                const prefixLength =
                    args.prefix.length;

                const targetLength =
                    args.target.length;

                const startWanted =
                    prefixLength;

                const endWanted =
                    prefixLength
                    + targetLength;

                const walker =
                    document.createTreeWalker(
                        root,
                        NodeFilter.SHOW_TEXT
                    );

                let absolute = 0;
                let startNode = null;
                let startOffset = 0;
                let endNode = null;
                let endOffset = 0;

                while (
                    walker.nextNode()
                ) {
                    const node =
                        walker.currentNode;

                    const value =
                        node.nodeValue || '';

                    const next =
                        absolute
                        + value.length;

                    if (
                        startNode === null
                        && startWanted >= absolute
                        && startWanted <= next
                    ) {
                        startNode = node;
                        startOffset =
                            startWanted
                            - absolute;
                    }

                    if (
                        endNode === null
                        && endWanted >= absolute
                        && endWanted <= next
                    ) {
                        endNode = node;
                        endOffset =
                            endWanted
                            - absolute;
                        break;
                    }

                    absolute = next;
                }

                if (
                    !startNode
                    || !endNode
                ) {
                    return {
                        ok: false,
                        reason:
                            'range-not-found',
                        text:
                            root.innerText || ''
                    };
                }

                const range =
                    document.createRange();

                range.setStart(
                    startNode,
                    startOffset
                );

                range.setEnd(
                    endNode,
                    endOffset
                );

                const selection =
                    window.getSelection();

                selection.removeAllRanges();
                selection.addRange(
                    range
                );

                document.dispatchEvent(
                    new Event(
                        'selectionchange',
                        {
                            bubbles: true
                        }
                    )
                );

                root.dispatchEvent(
                    new MouseEvent(
                        'mouseup',
                        {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }
                    )
                );

                return {
                    ok: true,
                    selected:
                        selection.toString()
                };
            }
            """,
            {
                "prefix": prefix,
                "target": target,
            },
        )

        if not result.get(
            "ok"
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "не удалось выделить диапазон: "
                + str(result)
            )

        return str(
            result.get(
                "selected",
                "",
            )
        )

    async def inspect_article(
        self,
        *,
        profile_dir: str,
        studio_url: str,
        article_title: str,
        source_body: str,
    ) -> dict:
        """
        DRY RUN.

        Открывает нужную статью,
        проверяет сопоставление Draft.js
        и доступные toolbar actions.

        НИЧЕГО НЕ ФОРМАТИРУЕТ
        И НЕ НАЖИМАЕТ «ОПУБЛИКОВАТЬ».
        """

        parsed = parse_dzen_markup(
            source_body
        )

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = (
                    await pw.chromium
                    .launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=self.headless,
                        viewport={
                            "width": 1440,
                            "height": 1200,
                        },
                        locale="ru-RU",
                    )
                )

                try:
                    page = (
                        context.pages[0]
                        if context.pages
                        else await context.new_page()
                    )

                    await self._goto_publications(
                        page,
                        studio_url,
                    )

                    body = await self._open_editor(
                        page,
                        article_title,
                    )

                    mapping = (
                        await self._map_lines_to_blocks(
                            body,
                            parsed,
                        )
                    )

                    actions = []

                    for span in parsed.spans:
                        actions.append({
                            "style": span.style,
                            "line": span.line,
                            "block": mapping.get(
                                span.line
                            ),
                            "text": span.text,
                            "supported": (
                                span.style
                                in _DZEN_TOOLBAR_LABELS
                            ),
                        })

                    for line_index in sorted(
                        parsed.blockquotes
                    ):
                        actions.append({
                            "style": "blockquote",
                            "line": line_index,
                            "block": mapping.get(
                                line_index
                            ),
                            "text": (
                                parsed.lines[
                                    line_index
                                ]
                            ),
                            "supported": True,
                        })

                    # Выделяем первый поддерживаемый
                    # inline-span только для появления
                    # toolbar. Это НЕ меняет статью.
                    first_span = next(
                        (
                            span
                            for span in parsed.spans
                            if (
                                span.style
                                in _DZEN_TOOLBAR_LABELS
                            )
                        ),
                        None,
                    )

                    visible_tools = []

                    if first_span is not None:
                        block_index = mapping[
                            first_span.line
                        ]

                        block = body.locator(
                            '[data-block="true"]'
                        ).nth(
                            block_index
                        )

                        plain_line = (
                            parsed.lines[
                                first_span.line
                            ]
                        )

                        selected = (
                            await self._select_text(
                                block,
                                prefix=plain_line[
                                    :first_span.start
                                ],
                                target=first_span.text,
                            )
                        )

                        if (
                            selected
                            != first_span.text
                        ):
                            raise RuntimeError(
                                "Dzen formatter: "
                                "контроль выделения не совпал. "
                                f"Ожидалось "
                                f"{first_span.text!r}, "
                                f"получено {selected!r}"
                            )

                        await page.wait_for_timeout(
                            400
                        )

                        for label in (
                            "Bold",
                            "Italic",
                            "Strike",
                            "Blockquote",
                            "Underline",
                        ):
                            locator = page.locator(
                                f'[aria-label="{label}"]'
                            )

                            found = False

                            for i in range(
                                await locator.count()
                            ):
                                try:
                                    if (
                                        await locator.nth(
                                            i
                                        ).is_visible()
                                    ):
                                        found = True
                                        break
                                except Exception:
                                    pass

                            if found:
                                visible_tools.append(
                                    label
                                )

                    return {
                        "editor_url": page.url,
                        "mapped_lines": len(
                            mapping
                        ),
                        "actions": actions,
                        "visible_tools": visible_tools,
                    }

                finally:
                    await context.close()


    async def _visible_toolbar_tool(
        self,
        page: Page,
        label: str,
    ):
        locator = page.locator(
            f'[aria-label="{label}"]'
        )

        for index in range(
            await locator.count()
        ):
            item = locator.nth(
                index
            )

            try:
                if await item.is_visible():
                    return item
            except Exception:
                pass

        return None

    async def _toolbar_tool_active(
        self,
        tool,
    ) -> bool:
        class_name = (
            await tool.get_attribute(
                "class"
            )
            or ""
        )

        aria_pressed = (
            await tool.get_attribute(
                "aria-pressed"
            )
            or ""
        ).lower()

        return (
            "active" in class_name.lower()
            or aria_pressed == "true"
        )

    async def _select_span(
        self,
        *,
        body,
        parsed: DzenParsedMarkup,
        mapping: dict[int, int],
        span: DzenFormatSpan,
    ) -> None:
        block_index = mapping[
            span.line
        ]

        block = body.locator(
            '[data-block="true"]'
        ).nth(
            block_index
        )

        line = parsed.lines[
            span.line
        ]

        selected = await self._select_text(
            block,
            prefix=line[
                :span.start
            ],
            target=span.text,
        )

        if selected != span.text:
            raise RuntimeError(
                "Dzen formatter: "
                "выделенный текст не совпал. "
                f"Ожидалось {span.text!r}, "
                f"получено {selected!r}"
            )

    async def _inline_style_active_in_dom(
        self,
        *,
        page: Page,
        body,
        parsed: DzenParsedMarkup,
        mapping: dict[int, int],
        span: DzenFormatSpan,
    ) -> bool:
        """
        Проверяет реальный CSS нужного диапазона Draft.js.

        Это надёжнее active-класса toolbar:
        toolbar может перерисовываться после изменения
        Draft.js и временно не отражать состояние selection.
        """

        block_index = mapping[
            span.line
        ]

        block = body.locator(
            '[data-block="true"]'
        ).nth(
            block_index
        )

        line = parsed.lines[
            span.line
        ]

        prefix = line[
            :span.start
        ]

        result = await block.evaluate(
            """
            (root, args) => {
                const startWanted =
                    args.prefix.length;

                const endWanted =
                    startWanted
                    + args.target.length;

                const walker =
                    document.createTreeWalker(
                        root,
                        NodeFilter.SHOW_TEXT
                    );

                let absolute = 0;
                let checked = 0;
                let active = true;

                const details = [];

                while (
                    walker.nextNode()
                ) {
                    const node =
                        walker.currentNode;

                    const value =
                        node.nodeValue || '';

                    const next =
                        absolute
                        + value.length;

                    const overlapStart =
                        Math.max(
                            startWanted,
                            absolute
                        );

                    const overlapEnd =
                        Math.min(
                            endWanted,
                            next
                        );

                    if (
                        overlapEnd
                        > overlapStart
                    ) {
                        const localStart =
                            overlapStart
                            - absolute;

                        const localEnd =
                            overlapEnd
                            - absolute;

                        const part =
                            value.slice(
                                localStart,
                                localEnd
                            );

                        if (
                            part.trim()
                        ) {
                            checked += 1;

                            const el =
                                node.parentElement;

                            const css =
                                el
                                ? getComputedStyle(el)
                                : null;

                            let ok = false;

                            if (css) {
                                if (
                                    args.style
                                    === 'bold'
                                ) {
                                    const weight =
                                        parseInt(
                                            css.fontWeight,
                                            10
                                        );

                                    ok = (
                                        Number.isFinite(
                                            weight
                                        )
                                        && weight >= 600
                                    )
                                    || css.fontWeight
                                        === 'bold'
                                    || css.fontWeight
                                        === 'bolder';
                                }

                                else if (
                                    args.style
                                    === 'italic'
                                ) {
                                    ok = (
                                        css.fontStyle
                                            === 'italic'
                                        || css.fontStyle
                                            === 'oblique'
                                    );
                                }

                                else if (
                                    args.style
                                    === 'strike'
                                ) {
                                    // text-decoration в CSS ведёт себя
                                    // не как обычное наследуемое свойство.
                                    //
                                    // Dzen/Draft.js может повесить
                                    // line-through на wrapper выше
                                    // непосредственного parentElement
                                    // текстового узла.
                                    let current =
                                        node.parentElement;

                                    while (current) {
                                        const currentCss =
                                            getComputedStyle(
                                                current
                                            );

                                        const decorationLine =
                                            (
                                                currentCss
                                                    .textDecorationLine
                                                || ''
                                            );

                                        const decoration =
                                            (
                                                currentCss
                                                    .textDecoration
                                                || ''
                                            );

                                        const tag =
                                            (
                                                current.tagName
                                                || ''
                                            ).toUpperCase();

                                        if (
                                            decorationLine.includes(
                                                'line-through'
                                            )
                                            || decoration.includes(
                                                'line-through'
                                            )
                                            || tag === 'S'
                                            || tag === 'DEL'
                                            || tag === 'STRIKE'
                                        ) {
                                            ok = true;
                                            break;
                                        }

                                        if (
                                            current === root
                                        ) {
                                            break;
                                        }

                                        current =
                                            current.parentElement;
                                    }
                                }
                            }

                            details.push({
                                text: part,
                                ok: ok,
                                weight:
                                    css
                                    ? css.fontWeight
                                    : '',
                                fontStyle:
                                    css
                                    ? css.fontStyle
                                    : '',
                                decoration:
                                    css
                                    ? css.textDecorationLine
                                    : ''
                            });

                            if (!ok) {
                                active = false;
                            }
                        }
                    }

                    absolute = next;

                    if (
                        absolute
                        >= endWanted
                    ) {
                        break;
                    }
                }

                return {
                    active:
                        checked > 0
                        && active,
                    checked,
                    details
                };
            }
            """,
            {
                "prefix": prefix,
                "target": span.text,
                "style": span.style,
            },
        )

        log.debug(
            "Dzen formatter DOM check: "
            "style=%s text=%r result=%s",
            span.style,
            span.text[:100],
            result,
        )

        return bool(
            result.get(
                "active"
            )
        )

    async def _ensure_inline_style(
        self,
        *,
        page: Page,
        body,
        parsed: DzenParsedMarkup,
        mapping: dict[int, int],
        span: DzenFormatSpan,
    ) -> str:
        label = _DZEN_TOOLBAR_LABELS.get(
            span.style
        )

        if not label:
            return "unsupported"

        # ------------------------------------------
        # Сначала проверяем сам DOM.
        # Если стиль уже есть — второй раз кнопку
        # не нажимаем.
        # ------------------------------------------

        if await self._inline_style_active_in_dom(
            page=page,
            body=body,
            parsed=parsed,
            mapping=mapping,
            span=span,
        ):
            return "already_active"

        await self._select_span(
            body=body,
            parsed=parsed,
            mapping=mapping,
            span=span,
        )

        await page.wait_for_timeout(
            250
        )

        tool = await self._visible_toolbar_tool(
            page,
            label,
        )

        if tool is None:
            raise RuntimeError(
                "Dzen formatter: "
                f"toolbar {label!r} не найден"
            )

        await tool.click(
            force=True
        )

        await page.wait_for_timeout(
            450
        )

        # Draft.js после клика перерисовывает
        # внутренние spans. Получаем body заново.
        body = page.locator(
            '[contenteditable="true"]'
            '.public-DraftEditor-content'
        ).nth(1)

        mapping = (
            await self._map_lines_to_blocks(
                body,
                parsed,
            )
        )

        # ------------------------------------------
        # Подтверждаем не toolbar-классом,
        # а реальным CSS нужного текста.
        # ------------------------------------------

        if not await self._inline_style_active_in_dom(
            page=page,
            body=body,
            parsed=parsed,
            mapping=mapping,
            span=span,
        ):
            raise RuntimeError(
                "Dzen formatter: "
                f"стиль {span.style!r} "
                "не появился в DOM после применения"
            )

        return "applied"

    async def _ensure_blockquote(
        self,
        *,
        page: Page,
        body,
        parsed: DzenParsedMarkup,
        mapping: dict[int, int],
        line_index: int,
    ) -> str:
        block_index = mapping[
            line_index
        ]

        body = page.locator(
            '[contenteditable="true"]'
            '.public-DraftEditor-content'
        ).nth(1)

        block = body.locator(
            '[data-block="true"]'
        ).nth(
            block_index
        )

        current_class = (
            await block.get_attribute(
                "class"
            )
            or ""
        )

        if (
            "zen-editor-block-quote"
            in current_class
        ):
            return "already_active"

        target = parsed.lines[
            line_index
        ]

        selected = await self._select_text(
            block,
            prefix="",
            target=target,
        )

        if selected != target:
            raise RuntimeError(
                "Dzen formatter: "
                "не удалось выделить цитату"
            )

        await page.wait_for_timeout(
            250
        )

        tool = await self._visible_toolbar_tool(
            page,
            "Blockquote",
        )

        if tool is None:
            raise RuntimeError(
                "Dzen formatter: "
                "Blockquote не найден"
            )

        if not await self._toolbar_tool_active(
            tool
        ):
            await tool.click(
                force=True
            )

            await page.wait_for_timeout(
                400
            )

        body = page.locator(
            '[contenteditable="true"]'
            '.public-DraftEditor-content'
        ).nth(1)

        block = body.locator(
            '[data-block="true"]'
        ).nth(
            block_index
        )

        final_class = (
            await block.get_attribute(
                "class"
            )
            or ""
        )

        if (
            "zen-editor-block-quote"
            not in final_class
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "Blockquote не подтвердился"
            )

        return "applied"

    async def _publish_editor_changes(
        self,
        page: Page,
    ) -> None:
        """
        Сохраняет изменения уже опубликованной
        статьи Dzen.

        Для существующей статьи Dzen использует
        двухшаговый сценарий:

        1. «Опубликовать»
        2. «Сохранить изменения»

        Успех подтверждаем только после исчезновения
        статуса «Есть неопубликованные правки».
        """

        async def find_visible_button(
            text: str,
        ):
            buttons = page.locator(
                "button"
            )

            for index in range(
                await buttons.count()
            ):
                button = buttons.nth(
                    index
                )

                try:
                    if not await button.is_visible():
                        continue

                    value = " ".join(
                        (
                            await button.inner_text()
                        ).split()
                    )

                    if value == text:
                        return button

                except Exception:
                    pass

            return None

        async def get_status() -> str:
            locator = page.locator(
                '[class*="editor-header__status"]'
            )

            for index in range(
                await locator.count()
            ):
                item = locator.nth(
                    index
                )

                try:
                    if not await item.is_visible():
                        continue

                    return " ".join(
                        (
                            await item.inner_text()
                        ).split()
                    )

                except Exception:
                    pass

            return ""

        # ------------------------------------------
        # ШАГ 1 — открываем финальный этап публикации
        # ------------------------------------------

        publish = await find_visible_button(
            "Опубликовать"
        )

        if publish is None:
            raise RuntimeError(
                "Dzen formatter: "
                "кнопка «Опубликовать» не найдена"
            )

        await publish.click(
            force=True
        )

        # ------------------------------------------
        # ШАГ 2 — ждём «Сохранить изменения»
        # ------------------------------------------

        save = None

        for _ in range(20):
            save = await find_visible_button(
                "Сохранить изменения"
            )

            if save is not None:
                break

            await page.wait_for_timeout(
                250
            )

        if save is None:
            status = await get_status()

            # В некоторых версиях Dzen первый клик
            # может сразу завершить обновление.
            if (
                "Есть неопубликованные правки"
                not in status
            ):
                log.info(
                    "Dzen formatter: изменения "
                    "сохранены после первого шага"
                )
                return

            raise RuntimeError(
                "Dzen formatter: "
                "кнопка «Сохранить изменения» "
                "не появилась"
            )

        await save.click(
            force=True
        )

        # ------------------------------------------
        # Подтверждаем реальное сохранение.
        # Просто успешного click() недостаточно.
        # ------------------------------------------

        last_status = ""

        for _ in range(30):
            await page.wait_for_timeout(
                250
            )

            last_status = await get_status()

            if (
                "Есть неопубликованные правки"
                not in last_status
            ):
                log.info(
                    "Dzen formatter: изменения "
                    "опубликованной статьи сохранены"
                )
                return

        raise RuntimeError(
            "Dzen formatter: "
            "после сохранения остался статус "
            "«Есть неопубликованные правки». "
            f"Текущий статус: {last_status!r}"
        )

    async def replace_article_body(
        self,
        *,
        profile_dir: str,
        studio_url: str,
        article_title: str,
        source_body: str,
        article_href: str | None = None,
        publish: bool = True,
    ) -> dict:
        """
        Полностью заменяет импортированное тело статьи Dzen
        исходным LONG-текстом.

        Служебная rich-разметка [[B]], [[I]], [[U]],
        [[S]], [[Q]] и Markdown-маркеры в редактор
        не вставляются.

        Форматирование применяется отдельно через
        format_article().
        """

        parsed = parse_dzen_markup(
            source_body
        )

        if not any(
            line.strip()
            for line in parsed.lines
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "пустой source_body для замены"
            )

        result = {
            "published": False,
            "old_body_chars": 0,
            "new_body_chars": 0,
            "mapped_lines": 0,
        }

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = (
                    await pw.chromium
                    .launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=self.headless,
                        viewport={
                            "width": 1440,
                            "height": 1200,
                        },
                        locale="ru-RU",
                    )
                )

                try:
                    page = (
                        context.pages[0]
                        if context.pages
                        else await context.new_page()
                    )

                    await self._goto_publications(
                        page,
                        studio_url,
                    )

                    body = await self._open_editor(
                        page,
                        article_title,
                        article_href=article_href,
                    )

                    old_text = (
                        await body.inner_text()
                    )

                    result[
                        "old_body_chars"
                    ] = len(old_text)

                    # ----------------------------------------
                    # УДАЛЯЕМ ТОЛЬКО ТЕКСТ, СОХРАНЯЯ MEDIA
                    # ----------------------------------------
                    #
                    # Telegram -> Dzen импортирует изображение
                    # отдельным Draft.js-блоком:
                    #
                    # figure.zen-editor-block-image
                    #
                    # Раньше selectNodeContents(root) выделял
                    # также этот figure, поэтому Backspace
                    # удалял картинку вместе с SHORT.
                    #
                    # Теперь выделяем только всё содержимое
                    # ДО первого image-блока.
                    # ----------------------------------------

                    image_blocks_before = await body.locator(
                        "figure.zen-editor-block-image"
                    ).count()

                    result[
                        "image_blocks_before"
                    ] = image_blocks_before

                    image_was_present = await body.evaluate(
                        """
                        root => {
                            root.focus();

                            const image = root.querySelector(
                                "figure.zen-editor-block-image"
                            );

                            const range =
                                document.createRange();

                            if (image) {
                                range.setStart(
                                    root,
                                    0
                                );

                                range.setEndBefore(
                                    image
                                );
                            } else {
                                range.selectNodeContents(
                                    root
                                );
                            }

                            const selection =
                                window.getSelection();

                            selection.removeAllRanges();
                            selection.addRange(
                                range
                            );

                            return Boolean(
                                image
                            );
                        }
                        """
                    )

                    await page.keyboard.press(
                        "Backspace"
                    )

                    await page.wait_for_timeout(
                        700
                    )

                    # Draft.js после удаления
                    # перерисовывает редактор.
                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    image_blocks_after_delete = (
                        await body.locator(
                            "figure.zen-editor-block-image"
                        ).count()
                    )

                    if (
                        image_was_present
                        and image_blocks_after_delete == 0
                    ):
                        raise RuntimeError(
                            "Dzen formatter: "
                            "изображение исчезло при очистке body"
                        )

                    # После Backspace Draft.js обычно оставляет
                    # пустой paragraph непосредственно перед
                    # media-блоком. Ставим caret именно в первый
                    # текстовый Draft-блок, не кликая по картинке.
                    caret_ready = await body.evaluate(
                        """
                        root => {
                            root.focus();

                            const blocks = Array.from(
                                root.querySelectorAll(
                                    '[data-block="true"]'
                                )
                            );

                            const textBlock = blocks.find(
                                block =>
                                    !block.matches(
                                        "figure.zen-editor-block-image"
                                    )
                                    && !block.closest(
                                        "figure.zen-editor-block-image"
                                    )
                            );

                            if (!textBlock) {
                                return false;
                            }

                            const range =
                                document.createRange();

                            range.selectNodeContents(
                                textBlock
                            );

                            range.collapse(
                                true
                            );

                            const selection =
                                window.getSelection();

                            selection.removeAllRanges();
                            selection.addRange(
                                range
                            );

                            return true;
                        }
                        """
                    )

                    if not caret_ready:
                        raise RuntimeError(
                            "Dzen formatter: "
                            "после очистки body не найден "
                            "текстовый Draft.js-блок "
                            "для вставки LONG"
                        )

                    # Вставляем уже очищенные от
                    # служебной разметки строки.
                    for index, line in enumerate(
                        parsed.lines
                    ):
                        if line:
                            await page.keyboard.insert_text(
                                line
                            )

                        if index < len(
                            parsed.lines
                        ) - 1:
                            await page.keyboard.press(
                                "Enter"
                            )

                    await page.wait_for_timeout(
                        1000
                    )

                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    new_text = (
                        await body.inner_text()
                    )

                    result[
                        "new_body_chars"
                    ] = len(new_text)

                    image_blocks_after = await body.locator(
                        "figure.zen-editor-block-image"
                    ).count()

                    result[
                        "image_blocks_after"
                    ] = image_blocks_after

                    result[
                        "image_preserved"
                    ] = (
                        image_blocks_before == 0
                        or image_blocks_after > 0
                    )

                    if (
                        image_blocks_before > 0
                        and image_blocks_after == 0
                    ):
                        raise RuntimeError(
                            "Dzen formatter: "
                            "изображение потеряно "
                            "после вставки LONG"
                        )

                    # Главная проверка целостности:
                    # каждая непустая строка source_body
                    # должна существовать в Draft.js
                    # в правильном порядке.
                    mapping = (
                        await self._map_lines_to_blocks(
                            body,
                            parsed,
                        )
                    )

                    result[
                        "mapped_lines"
                    ] = len(mapping)

                    if publish:
                        await self._publish_editor_changes(
                            page
                        )

                        result[
                            "published"
                        ] = True

                    result[
                        "editor_url"
                    ] = page.url

                    log.info(
                        "Dzen formatter: body заменён "
                        "title=%r old_chars=%s "
                        "new_chars=%s mapped=%s "
                        "published=%s",
                        article_title,
                        result["old_body_chars"],
                        result["new_body_chars"],
                        result["mapped_lines"],
                        result["published"],
                    )

                    return result

                finally:
                    await context.close()



    async def finalize_article(
        self,
        *,
        profile_dir: str,
        studio_url: str,
        article_title: str,
        source_body: str,
        article_href: str | None = None,
        publish: bool = True,
    ) -> dict:
        """
        Финализирует импортированную Dzen-статью
        за одну браузерную/editor-сессию:

        1. открывает статью;
        2. сохраняет импортированные media-блоки;
        3. заменяет seed-текст на полный LONG;
        4. применяет rich-format;
        5. проверяет текст и изображения;
        6. сохраняет изменения один раз.
        """

        parsed = parse_dzen_markup(
            source_body
        )

        if not any(
            line.strip()
            for line in parsed.lines
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "пустой source_body для finalize"
            )

        result = {
            "body_replaced": False,
            "formatted": False,
            "published": False,
            "old_body_chars": 0,
            "new_body_chars": 0,
            "mapped_lines": 0,
            "image_blocks_before": 0,
            "image_blocks_after": 0,
            "image_preserved": True,
            "applied": [],
            "already_active": [],
            "unsupported": [],
        }

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = (
                    await pw.chromium
                    .launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=self.headless,
                        viewport={
                            "width": 1440,
                            "height": 1200,
                        },
                        locale="ru-RU",
                    )
                )

                try:
                    page = (
                        context.pages[0]
                        if context.pages
                        else await context.new_page()
                    )

                    await self._goto_publications(
                        page,
                        studio_url,
                    )

                    body = await self._open_editor(
                        page,
                        article_title,
                        article_href=article_href,
                    )

                    # ======================================
                    # 1. REPLACE BODY, PRESERVE MEDIA
                    # ======================================

                    old_text = await body.inner_text()

                    result[
                        "old_body_chars"
                    ] = len(old_text)

                    image_blocks_before = (
                        await body.locator(
                            "figure.zen-editor-block-image"
                        ).count()
                    )

                    result[
                        "image_blocks_before"
                    ] = image_blocks_before

                    image_was_present = (
                        await body.evaluate(
                            """
                            root => {
                                root.focus();

                                const image =
                                    root.querySelector(
                                        "figure.zen-editor-block-image"
                                    );

                                const range =
                                    document.createRange();

                                if (image) {
                                    range.setStart(
                                        root,
                                        0
                                    );

                                    range.setEndBefore(
                                        image
                                    );
                                } else {
                                    range.selectNodeContents(
                                        root
                                    );
                                }

                                const selection =
                                    window.getSelection();

                                selection.removeAllRanges();
                                selection.addRange(
                                    range
                                );

                                return Boolean(
                                    image
                                );
                            }
                            """
                        )
                    )

                    await page.keyboard.press(
                        "Backspace"
                    )

                    await page.wait_for_timeout(
                        700
                    )

                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    image_blocks_after_delete = (
                        await body.locator(
                            "figure.zen-editor-block-image"
                        ).count()
                    )

                    if (
                        image_was_present
                        and image_blocks_after_delete == 0
                    ):
                        raise RuntimeError(
                            "Dzen formatter: "
                            "изображение исчезло "
                            "при очистке body"
                        )

                    caret_ready = await body.evaluate(
                        """
                        root => {
                            root.focus();

                            const blocks = Array.from(
                                root.querySelectorAll(
                                    '[data-block="true"]'
                                )
                            );

                            const textBlock = blocks.find(
                                block =>
                                    !block.matches(
                                        "figure.zen-editor-block-image"
                                    )
                                    && !block.closest(
                                        "figure.zen-editor-block-image"
                                    )
                            );

                            if (!textBlock) {
                                return false;
                            }

                            const range =
                                document.createRange();

                            range.selectNodeContents(
                                textBlock
                            );

                            range.collapse(
                                true
                            );

                            const selection =
                                window.getSelection();

                            selection.removeAllRanges();
                            selection.addRange(
                                range
                            );

                            return true;
                        }
                        """
                    )

                    if not caret_ready:
                        raise RuntimeError(
                            "Dzen formatter: "
                            "после очистки body "
                            "не найден текстовый блок"
                        )

                    for index, line in enumerate(
                        parsed.lines
                    ):
                        if line:
                            await page.keyboard.insert_text(
                                line
                            )

                        if index < (
                            len(parsed.lines) - 1
                        ):
                            await page.keyboard.press(
                                "Enter"
                            )

                    await page.wait_for_timeout(
                        1000
                    )

                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    new_text = await body.inner_text()

                    result[
                        "new_body_chars"
                    ] = len(new_text)

                    image_blocks_after = (
                        await body.locator(
                            "figure.zen-editor-block-image"
                        ).count()
                    )

                    result[
                        "image_blocks_after"
                    ] = image_blocks_after

                    result[
                        "image_preserved"
                    ] = (
                        image_blocks_before == 0
                        or image_blocks_after > 0
                    )

                    if (
                        image_blocks_before > 0
                        and image_blocks_after == 0
                    ):
                        raise RuntimeError(
                            "Dzen formatter: "
                            "изображение потеряно "
                            "после вставки LONG"
                        )

                    mapping = (
                        await self._map_lines_to_blocks(
                            body,
                            parsed,
                        )
                    )

                    result[
                        "mapped_lines"
                    ] = len(mapping)

                    result[
                        "body_replaced"
                    ] = True

                    log.info(
                        "Dzen formatter: finalize "
                        "body готов; old_chars=%s "
                        "new_chars=%s mapped=%s "
                        "images=%s→%s",
                        result["old_body_chars"],
                        result["new_body_chars"],
                        result["mapped_lines"],
                        image_blocks_before,
                        image_blocks_after,
                    )

                    # ======================================
                    # 2. INLINE FORMAT
                    # ======================================

                    for span in parsed.spans:
                        if (
                            span.style
                            not in _DZEN_TOOLBAR_LABELS
                        ):
                            result[
                                "unsupported"
                            ].append({
                                "style": span.style,
                                "text": span.text,
                            })

                            continue

                        body = page.locator(
                            '[contenteditable="true"]'
                            '.public-DraftEditor-content'
                        ).nth(1)

                        mapping = (
                            await self._map_lines_to_blocks(
                                body,
                                parsed,
                            )
                        )

                        status = (
                            await self._ensure_inline_style(
                                page=page,
                                body=body,
                                parsed=parsed,
                                mapping=mapping,
                                span=span,
                            )
                        )

                        result[
                            status
                        ].append({
                            "style": span.style,
                            "text": span.text,
                        })

                    # ======================================
                    # 3. BLOCKQUOTES
                    # ======================================

                    for line_index in sorted(
                        parsed.blockquotes
                    ):
                        body = page.locator(
                            '[contenteditable="true"]'
                            '.public-DraftEditor-content'
                        ).nth(1)

                        mapping = (
                            await self._map_lines_to_blocks(
                                body,
                                parsed,
                            )
                        )

                        status = (
                            await self._ensure_blockquote(
                                page=page,
                                body=body,
                                parsed=parsed,
                                mapping=mapping,
                                line_index=line_index,
                            )
                        )

                        result[
                            status
                        ].append({
                            "style": "blockquote",
                            "text": parsed.lines[
                                line_index
                            ],
                        })

                    # ======================================
                    # 4. FINAL INTEGRITY CHECK
                    # ======================================

                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    mapping = (
                        await self._map_lines_to_blocks(
                            body,
                            parsed,
                        )
                    )

                    result[
                        "mapped_lines"
                    ] = len(mapping)

                    final_image_count = (
                        await body.locator(
                            "figure.zen-editor-block-image"
                        ).count()
                    )

                    result[
                        "image_blocks_after"
                    ] = final_image_count

                    result[
                        "image_preserved"
                    ] = (
                        image_blocks_before == 0
                        or final_image_count > 0
                    )

                    if (
                        image_blocks_before > 0
                        and final_image_count == 0
                    ):
                        raise RuntimeError(
                            "Dzen formatter: "
                            "изображение потеряно "
                            "во время форматирования"
                        )

                    result[
                        "formatted"
                    ] = True

                    # ======================================
                    # 5. ONE FINAL SAVE
                    # ======================================

                    if publish:
                        await self._publish_editor_changes(
                            page
                        )

                        result[
                            "published"
                        ] = True

                    result[
                        "editor_url"
                    ] = page.url

                    log.info(
                        "Dzen formatter: finalize готов; "
                        "published=%s applied=%s "
                        "already_active=%s unsupported=%s "
                        "image_preserved=%s",
                        result["published"],
                        len(result["applied"]),
                        len(
                            result[
                                "already_active"
                            ]
                        ),
                        len(result["unsupported"]),
                        result["image_preserved"],
                    )

                    return result

                finally:
                    await context.close()


    async def format_article(
        self,
        *,
        profile_dir: str,
        studio_url: str,
        article_title: str,
        source_body: str,
        article_href: str | None = None,
        publish: bool = True,
    ) -> dict:
        """
        Применяет форматирование исходного LONG
        к уже синхронизированной статье Dzen.

        Поддерживается:
        - bold
        - italic
        - strike
        - blockquote

        underline сохраняет сам текст,
        но пропускается, если Dzen не имеет
        соответствующего toolbar action.
        """

        parsed = parse_dzen_markup(
            source_body
        )

        result = {
            "applied": [],
            "already_active": [],
            "unsupported": [],
            "published": False,
        }

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = (
                    await pw.chromium
                    .launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=self.headless,
                        viewport={
                            "width": 1440,
                            "height": 1200,
                        },
                        locale="ru-RU",
                    )
                )

                try:
                    page = (
                        context.pages[0]
                        if context.pages
                        else await context.new_page()
                    )

                    await self._goto_publications(
                        page,
                        studio_url,
                    )

                    body = await self._open_editor(
                        page,
                        article_title,
                        article_href=article_href,
                    )

                    await self._remove_synced_duplicate_title(
                        page=page,
                        body=body,
                        article_title=article_title,
                        parsed=parsed,
                    )

                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    mapping = (
                        await self._map_lines_to_blocks(
                            body,
                            parsed,
                        )
                    )

                    # -------------------------
                    # INLINE FORMAT
                    # -------------------------

                    for span in parsed.spans:
                        if (
                            span.style
                            not in _DZEN_TOOLBAR_LABELS
                        ):
                            result[
                                "unsupported"
                            ].append({
                                "style": span.style,
                                "text": span.text,
                            })

                            continue

                        body = page.locator(
                            '[contenteditable="true"]'
                            '.public-DraftEditor-content'
                        ).nth(1)

                        # Draft.js после каждого изменения
                        # может перерисовать структуру editor DOM.
                        # Поэтому старый mapping повторно
                        # не используем.
                        mapping = (
                            await self._map_lines_to_blocks(
                                body,
                                parsed,
                            )
                        )

                        status = (
                            await self._ensure_inline_style(
                                page=page,
                                body=body,
                                parsed=parsed,
                                mapping=mapping,
                                span=span,
                            )
                        )

                        result[
                            status
                        ].append({
                            "style": span.style,
                            "text": span.text,
                        })

                    # -------------------------
                    # BLOCKQUOTES
                    # -------------------------

                    for line_index in sorted(
                        parsed.blockquotes
                    ):
                        body = page.locator(
                            '[contenteditable="true"]'
                            '.public-DraftEditor-content'
                        ).nth(1)

                        mapping = (
                            await self._map_lines_to_blocks(
                                body,
                                parsed,
                            )
                        )

                        status = (
                            await self._ensure_blockquote(
                                page=page,
                                body=body,
                                parsed=parsed,
                                mapping=mapping,
                                line_index=line_index,
                            )
                        )

                        result[
                            status
                        ].append({
                            "style": "blockquote",
                            "text": parsed.lines[
                                line_index
                            ],
                        })

                    # -------------------------
                    # TEXT INTEGRITY CHECK
                    # -------------------------

                    body = page.locator(
                        '[contenteditable="true"]'
                        '.public-DraftEditor-content'
                    ).nth(1)

                    # Если хоть одна исходная строка
                    # потерялась — НЕ публикуем.
                    await self._map_lines_to_blocks(
                        body,
                        parsed,
                    )

                    if publish:
                        await self._publish_editor_changes(
                            page
                        )

                        result[
                            "published"
                        ] = True

                    result[
                        "editor_url"
                    ] = page.url

                    return result

                finally:
                    await context.close()


    async def _remove_synced_duplicate_title(
        self,
        *,
        page: Page,
        body,
        article_title: str,
        parsed: DzenParsedMarkup,
    ) -> bool:
        """
        Удаляет заголовок, который Dzen-синхронизация
        дополнительно поместила первым блоком body.

        ВАЖНО:
        если пользователь сам явно поместил article_title
        первой строкой source_body — ничего не удаляем.
        """

        title_norm = _normalize_dzen_text(
            article_title
        )

        if not title_norm:
            return False

        # ------------------------------------------
        # Проверяем исходный текст пользователя.
        # Если там заголовок действительно был частью
        # body, считаем это намеренным.
        # ------------------------------------------

        source_first = ""

        for line in parsed.lines:
            value = _normalize_dzen_text(
                line
            )

            if value:
                source_first = value
                break

        if source_first == title_norm:
            log.info(
                "Dzen formatter: article_title "
                "явно присутствует в source_body; "
                "не удаляю его"
            )
            return False

        blocks = body.locator(
            '[data-block="true"]'
        )

        count = await blocks.count()

        first_index = None
        next_index = None

        for index in range(
            count
        ):
            text = _normalize_dzen_text(
                await blocks.nth(
                    index
                ).inner_text()
            )

            if (
                first_index is None
                and text
            ):
                first_index = index

                if text != title_norm:
                    return False

                continue

            if (
                first_index is not None
                and text
            ):
                next_index = index
                break

        if first_index is None:
            return False

        if next_index is None:
            raise RuntimeError(
                "Dzen formatter: "
                "найден дублирующий заголовок, "
                "но следующий блок body отсутствует"
            )

        first_block = blocks.nth(
            first_index
        )

        next_block = blocks.nth(
            next_index
        )

        next_text = _normalize_dzen_text(
            await next_block.inner_text()
        )

        # ------------------------------------------
        # Выделяем диапазон:
        #
        # начало дублирующего title
        #       ↓
        # начало первого настоящего блока body
        #
        # Таким образом удаляется и title,
        # и пустые абзацы между ним и body.
        # ------------------------------------------

        result = await body.evaluate(
            """
            (root, args) => {
                const blocks =
                    root.querySelectorAll(
                        '[data-block="true"]'
                    );

                const first =
                    blocks[args.firstIndex];

                const next =
                    blocks[args.nextIndex];

                if (!first || !next) {
                    return {
                        ok: false,
                        reason: 'block-not-found'
                    };
                }

                function firstTextNode(element) {
                    const walker =
                        document.createTreeWalker(
                            element,
                            NodeFilter.SHOW_TEXT
                        );

                    while (
                        walker.nextNode()
                    ) {
                        return walker.currentNode;
                    }

                    return null;
                }

                const firstNode =
                    firstTextNode(first);

                const nextNode =
                    firstTextNode(next);

                if (
                    !firstNode
                    || !nextNode
                ) {
                    return {
                        ok: false,
                        reason: 'text-node-not-found'
                    };
                }

                root.focus();

                const range =
                    document.createRange();

                range.setStart(
                    firstNode,
                    0
                );

                range.setEnd(
                    nextNode,
                    0
                );

                const selection =
                    window.getSelection();

                selection.removeAllRanges();
                selection.addRange(
                    range
                );

                document.dispatchEvent(
                    new Event(
                        'selectionchange',
                        {
                            bubbles: true
                        }
                    )
                );

                return {
                    ok: true,
                    selected:
                        selection.toString()
                };
            }
            """,
            {
                "firstIndex": first_index,
                "nextIndex": next_index,
            },
        )

        if not result.get(
            "ok"
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "не удалось выделить "
                "дублирующий заголовок: "
                + str(result)
            )

        await page.keyboard.press(
            "Backspace"
        )

        await page.wait_for_timeout(
            500
        )

        # Draft.js перерисовался.
        body = page.locator(
            '[contenteditable="true"]'
            '.public-DraftEditor-content'
        ).nth(1)

        blocks = body.locator(
            '[data-block="true"]'
        )

        remaining = []

        for index in range(
            await blocks.count()
        ):
            value = _normalize_dzen_text(
                await blocks.nth(
                    index
                ).inner_text()
            )

            if value:
                remaining.append(
                    value
                )

        if (
            remaining
            and remaining[0]
            == title_norm
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "дублирующий заголовок "
                "остался после удаления"
            )

        if (
            next_text
            and next_text not in remaining
        ):
            raise RuntimeError(
                "Dzen formatter: "
                "при удалении заголовка "
                "потерялся первый блок статьи"
            )

        log.info(
            "Dzen formatter: "
            "дублирующий title удалён из body"
        )

        return True


    async def wait_for_article(
        self,
        *,
        profile_dir: str,
        studio_url: str,
        article_title: str,
        timeout_seconds: int = 180,
        poll_seconds: int = 5,
    ) -> str | None:
        """
        Ждёт появления статьи в Dzen Studio.

        Возвращает href статьи сразу после того,
        как публикация появилась в списке.

        Если статья не появилась за timeout —
        возвращает None.

        Не редактирует и не публикует статью.
        """

        deadline = (
            asyncio.get_running_loop().time()
            + timeout_seconds
        )

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = (
                    await pw.chromium
                    .launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=self.headless,
                        viewport={
                            "width": 1440,
                            "height": 1200,
                        },
                        locale="ru-RU",
                    )
                )

                try:
                    page = (
                        context.pages[0]
                        if context.pages
                        else await context.new_page()
                    )

                    await self._goto_publications(
                        page,
                        studio_url,
                    )

                    attempt = 0

                    while True:
                        attempt += 1

                        try:
                            article = (
                                await self._find_article_link(
                                    page,
                                    article_title,
                                )
                            )

                            if (
                                article is not None
                                and await article.count() > 0
                            ):
                                article_href = str(
                                    await article.get_attribute(
                                        "href"
                                    )
                                    or ""
                                ).strip()

                                if not article_href:
                                    raise RuntimeError(
                                        "Dzen formatter: "
                                        "у найденной статьи "
                                        "отсутствует href"
                                    )

                                log.info(
                                    "Dzen formatter: "
                                    "статья появилась в Dzen "
                                    "после попытки %s: %s; "
                                    "href=%s",
                                    attempt,
                                    article_title,
                                    article_href,
                                )

                                return article_href

                        except RuntimeError as exc:
                            # Нормальная ситуация:
                            # синхронизация ещё не завершена.
                            if (
                                "не найдена статья"
                                not in str(exc)
                            ):
                                log.debug(
                                    "Dzen wait: %s",
                                    exc,
                                )

                        now = (
                            asyncio
                            .get_running_loop()
                            .time()
                        )

                        if now >= deadline:
                            break

                        await page.wait_for_timeout(
                            poll_seconds * 1000
                        )

                        try:
                            await page.reload(
                                wait_until=(
                                    "domcontentloaded"
                                ),
                                timeout=30000,
                            )
                        except Exception:
                            pass

                    log.warning(
                        "Dzen formatter: "
                        "статья не появилась за %s сек: %s",
                        timeout_seconds,
                        article_title,
                    )

                    return None

                finally:
                    await context.close()
