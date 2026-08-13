from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


@dataclass(slots=True)
class WebPostRef:
    """
    Ссылка на опубликованный long-post внутри текущей Telegram Web-сессии.

    Telegram Web не даёт нам Bot API message_id, поэтому сохраняем:
    - найденный DOM-атрибут сообщения, если он есть;
    - короткий фрагмент текста как fallback;
    - время публикации.
    """
    dom_attr: str | None = None
    dom_value: str | None = None
    text_hint: str = ""
    created_at: float = 0.0

    def serialize(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def deserialize(cls, raw: str) -> "WebPostRef":
        data = json.loads(raw)
        return cls(**data)


class TelegramWebPublisher:
    """
    Публикует standard media post через Telegram Web K от уже
    авторизованного Premium-пользователя.

    Runtime рассчитан на Ubuntu VPS / systemd / headless Chromium.
    """

    def __init__(
        self,
        *,
        channel: str,
        profile_dir: Path,
        headless: bool,
        web_url: str,
        debug_dir: Path,
        action_timeout_ms: int,
    ):
        self.channel = channel.lstrip("@").strip()
        self.profile_dir = profile_dir
        self.headless = headless
        self.web_url = web_url.rstrip("/") + "/"
        self.debug_dir = debug_dir
        self.action_timeout_ms = action_timeout_ms

        if not self.channel:
            raise RuntimeError("Не задан TG_WEB_CHANNEL")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "TelegramWebPublisher":
        profile_raw = os.getenv(
            "TG_WEB_PROFILE_DIR",
            "data/telegram_web_profile",
        ).strip()
        debug_raw = os.getenv(
            "TG_WEB_DEBUG_DIR",
            "data/telegram_web_debug",
        ).strip()

        profile_dir = Path(profile_raw)
        if not profile_dir.is_absolute():
            profile_dir = BASE_DIR / profile_dir

        debug_dir = Path(debug_raw)
        if not debug_dir.is_absolute():
            debug_dir = BASE_DIR / debug_dir

        return cls(
            channel=os.getenv(
                "TG_WEB_CHANNEL",
                "boykov_nikolay",
            ),
            profile_dir=profile_dir,
            headless=os.getenv(
                "TG_WEB_HEADLESS",
                "true",
            ).strip().lower() not in {
                "0", "false", "no", "off"
            },
            web_url=os.getenv(
                "TG_WEB_URL",
                "https://web.telegram.org/k/",
            ),
            debug_dir=debug_dir,
            action_timeout_ms=int(
                os.getenv(
                    "TG_WEB_ACTION_TIMEOUT_MS",
                    "20000",
                )
            ),
        )

    async def _ensure_browser(self) -> tuple[BrowserContext, Page]:
        if self._context is not None and self._page is not None:
            if not self._page.is_closed():
                return self._context, self._page

        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.set_default_timeout(self.action_timeout_ms)

        return self._context, self._page

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            self._page = None

            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def _debug_capture(
        self,
        page: Page,
        label: str,
    ) -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(
            r"[^a-zA-Z0-9_-]+",
            "_",
            label,
        )[:60]

        png = self.debug_dir / f"{stamp}_{safe}.png"
        html = self.debug_dir / f"{stamp}_{safe}.html"

        try:
            await page.screenshot(
                path=str(png),
                full_page=True,
            )
        except Exception:
            log.exception(
                "Не удалось сохранить Telegram Web screenshot"
            )

        try:
            html.write_text(
                await page.content(),
                encoding="utf-8",
            )
        except Exception:
            log.exception(
                "Не удалось сохранить Telegram Web HTML"
            )

        log.error(
            "Telegram Web debug сохранён: %s / %s",
            png,
            html,
        )

    @staticmethod
    async def _first_visible(
        locators: Iterable[Locator],
    ) -> Locator | None:
        for locator in locators:
            try:
                count = await locator.count()
            except Exception:
                continue

            for index in range(min(count, 12)):
                item = locator.nth(index)
                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    continue

        return None

    async def _is_authorized(
        self,
        page: Page,
    ) -> bool:
        authorized_markers = [
            page.locator(
                '.chatlist, .chatlist-container, .sidebar-left'
            ),
            page.locator(
                'input[placeholder*="Search" i]'
            ),
            page.locator(
                'input[placeholder*="Поиск" i]'
            ),
            page.locator(
                '.input-search-input'
            ),
        ]

        marker = await self._first_visible(
            authorized_markers
        )

        if marker is not None:
            return True

        # На странице логина обычно есть QR / phone-login controls.
        login_markers = [
            page.get_by_text(
                re.compile(
                    r"log in by phone|войти по номеру",
                    re.I,
                )
            ),
            page.locator(
                'input[type="tel"]'
            ),
            page.locator(
                'canvas'
            ),
        ]

        login_marker = await self._first_visible(
            login_markers
        )

        return login_marker is None and (
            "web.telegram.org" in page.url
            and "login" not in page.url.lower()
        )

    async def _require_authorized(
        self,
        page: Page,
    ) -> None:
        if await self._is_authorized(page):
            return

        await self._debug_capture(
            page,
            "not_authorized",
        )

        raise RuntimeError(
            "Telegram Web не авторизован. "
            "Сначала выполните на VPS: "
            "source venv/bin/activate && python setup_telegram_web.py"
        )

    async def healthcheck(self) -> dict[str, str | bool]:
        async with self._lock:
            _, page = await self._ensure_browser()

            await page.goto(
                self.web_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(2500)

            authorized = await self._is_authorized(
                page
            )

            return {
                "authorized": authorized,
                "url": page.url,
                "channel": self.channel,
                "headless": self.headless,
                "profile_dir": str(self.profile_dir),
            }

    async def _goto_channel(
        self,
        page: Page,
    ) -> None:
        await self._require_authorized(
            page
        )

        channel_url = (
            f"{self.web_url}#@{self.channel}"
        )

        await page.goto(
            channel_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(2500)

        # Telegram Web — SPA. Иногда прямой hash-route не успевает.
        composer = await self._get_main_composer(
            page,
            optional=True,
        )

        if composer is not None:
            return

        # Fallback через поиск.
        search = await self._first_visible(
            [
                page.locator(
                    'input[placeholder*="Search" i]'
                ),
                page.locator(
                    'input[placeholder*="Поиск" i]'
                ),
                page.locator(
                    '.input-search-input'
                ),
            ]
        )

        if search is None:
            await self._debug_capture(
                page,
                "channel_open_no_search",
            )
            raise RuntimeError(
                "Не удалось открыть канал в Telegram Web: "
                "не найдено поле поиска."
            )

        await search.click()
        await search.fill(
            self.channel
        )
        await page.wait_for_timeout(1800)

        result = await self._first_visible(
            [
                page.get_by_text(
                    re.compile(
                        re.escape(
                            self.channel
                        ),
                        re.I,
                    )
                ),
                page.locator(
                    f'[data-peer-id*="{self.channel}"]'
                ),
            ]
        )

        if result is None:
            await self._debug_capture(
                page,
                "channel_not_found",
            )
            raise RuntimeError(
                f"Telegram Web не нашёл канал @{self.channel}"
            )

        await result.click()
        await page.wait_for_timeout(1800)

        composer = await self._get_main_composer(
            page,
            optional=True,
        )

        if composer is None:
            await self._debug_capture(
                page,
                "channel_open_no_composer",
            )
            raise RuntimeError(
                "Канал открыт, но Telegram Web не показывает поле "
                "публикации. Проверьте права аккаунта на публикацию."
            )

    async def _get_main_composer(
        self,
        page: Page,
        *,
        optional: bool = False,
    ) -> Locator | None:
        composer = await self._first_visible(
            [
                page.locator(
                    '.input-message-input[contenteditable="true"]'
                ),
                page.locator(
                    '.message-input [contenteditable="true"]'
                ),
                page.locator(
                    '[contenteditable="true"][data-placeholder*="Message" i]'
                ),
                page.locator(
                    '[contenteditable="true"][data-placeholder*="Сообщение" i]'
                ),
                page.locator(
                    'div[contenteditable="true"]'
                ),
            ]
        )

        if composer is None and not optional:
            await self._debug_capture(
                page,
                "main_composer_not_found",
            )
            raise RuntimeError(
                "Не найдено поле сообщения Telegram Web."
            )

        return composer

    async def _select_photo_input(
        self,
        page: Page,
        image_bytes: bytes,
    ) -> None:
        payload = {
            "name": "article.jpg",
            "mimeType": "image/jpeg",
            "buffer": image_bytes,
        }

        async def try_inputs() -> bool:
            inputs = page.locator(
                'input[type="file"]'
            )
            count = await inputs.count()

            selected: Locator | None = None

            for index in range(count):
                item = inputs.nth(index)
                accept = (
                    await item.get_attribute(
                        "accept"
                    )
                    or ""
                ).lower()

                # Приоритет — input для фото/видео.
                if (
                    "image" in accept
                    or "video" in accept
                ):
                    selected = item
                    break

            if selected is None and count:
                selected = inputs.nth(
                    count - 1
                )

            if selected is None:
                return False

            try:
                await selected.set_input_files(
                    payload
                )
                return True
            except Exception:
                return False

        if await try_inputs():
            return

        # Некоторые версии Telegram Web создают file input
        # только после открытия Attach.
        attach = await self._first_visible(
            [
                page.locator(
                    'button[aria-label*="Attach" i]'
                ),
                page.locator(
                    'button[aria-label*="Прикреп" i]'
                ),
                page.locator(
                    '.btn-icon.tgico-attach'
                ),
                page.locator(
                    '[data-tippy-content*="Attach" i]'
                ),
            ]
        )

        if attach is None:
            await self._debug_capture(
                page,
                "attach_not_found",
            )
            raise RuntimeError(
                "Не найдено управление Attach в Telegram Web."
            )

        await attach.click()
        await page.wait_for_timeout(400)

        if await try_inputs():
            return

        # Fallback: menu item "Photo or Video".
        photo_menu = await self._first_visible(
            [
                page.get_by_text(
                    re.compile(
                        r"photo|video|фото|видео",
                        re.I,
                    )
                ),
            ]
        )

        if photo_menu is None:
            await self._debug_capture(
                page,
                "photo_menu_not_found",
            )
            raise RuntimeError(
                "Не удалось открыть выбор фотографии в Telegram Web."
            )

        try:
            async with page.expect_file_chooser(
                timeout=5000
            ) as chooser_info:
                await photo_menu.click()

            chooser = await chooser_info.value
            await chooser.set_files(
                payload
            )
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(250)

            if not await try_inputs():
                await self._debug_capture(
                    page,
                    "file_chooser_failed",
                )
                raise RuntimeError(
                    "Telegram Web не открыл загрузку изображения."
                )

    async def _get_caption_editor(
        self,
        page: Page,
    ) -> Locator:
        # После выбора картинки на странице обычно два contenteditable:
        # обычный composer и caption в media popup. Берём самый нижний
        # видимый редактор.
        candidates = page.locator(
            'div[contenteditable="true"]'
        )
        count = await candidates.count()

        visible: list[
            tuple[float, Locator]
        ] = []

        for index in range(count):
            item = candidates.nth(index)
            try:
                if not await item.is_visible():
                    continue

                box = await item.bounding_box()
                if not box:
                    continue

                visible.append(
                    (
                        float(
                            box["y"]
                            + box["height"]
                        ),
                        item,
                    )
                )
            except Exception:
                continue

        if not visible:
            await self._debug_capture(
                page,
                "caption_editor_not_found",
            )
            raise RuntimeError(
                "Не найдено поле caption после выбора изображения."
            )

        visible.sort(
            key=lambda pair: pair[0],
            reverse=True,
        )

        return visible[0][1]

    async def _send_media_popup(
        self,
        page: Page,
        caption: str,
    ) -> None:
        editor = await self._get_caption_editor(
            page
        )

        await editor.click()
        await editor.fill(
            caption
        )
        await page.wait_for_timeout(250)

        # Заголовок — первая строка caption — оформляем жирным
        # средствами самого Telegram Web. Markdown-звёздочки в текст
        # не добавляются и в Дзен не передаются как символы.
        try:
            await editor.press("Control+Home")
            await editor.press("Shift+End")
            await editor.press("Control+b")
            await editor.press("End")
            await page.wait_for_timeout(150)
        except Exception:
            log.exception(
                "Не удалось применить жирное начертание к заголовку "
                "в Telegram Web; публикация продолжится без Markdown."
            )

        send = await self._first_visible(
            [
                page.locator(
                    'button[aria-label="Send"]'
                ),
                page.locator(
                    'button[aria-label="Отправить"]'
                ),
                page.locator(
                    '.popup-send-photo .btn-send'
                ),
                page.locator(
                    '.popup-send-media .btn-send'
                ),
                page.locator(
                    'button.btn-send'
                ),
                page.get_by_role(
                    "button",
                    name=re.compile(
                        r"^send$|^отправить$",
                        re.I,
                    ),
                ),
            ]
        )

        if send is None:
            await self._debug_capture(
                page,
                "send_button_not_found",
            )
            raise RuntimeError(
                "Не найдена кнопка Send в окне фотографии Telegram Web."
            )

        await send.click()
        await page.wait_for_timeout(2200)

    @staticmethod
    def _hint_from_caption(
        caption: str,
    ) -> str:
        hint = " ".join(
            caption.split()
        )

        if len(hint) > 90:
            hint = hint[:90].rsplit(
                " ",
                1,
            )[0]

        return hint

    async def _find_post_element(
        self,
        page: Page,
        *,
        ref: WebPostRef | None,
        caption_hint: str,
    ) -> Locator | None:
        if (
            ref
            and ref.dom_attr
            and ref.dom_value
        ):
            safe_value = ref.dom_value.replace(
                '"',
                '\\"',
            )
            locator = page.locator(
                f'[{ref.dom_attr}="{safe_value}"]'
            )
            if await locator.count():
                item = locator.last
                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    pass

        candidates = [
            page.locator(
                '.message.is-out'
            ),
            page.locator(
                '.message.out'
            ),
            page.locator(
                '.bubble.is-out'
            ),
            page.locator(
                '.bubble.own'
            ),
            page.locator(
                '[data-mid]'
            ),
            page.locator(
                '[data-message-id]'
            ),
        ]

        hint = (
            ref.text_hint
            if ref and ref.text_hint
            else caption_hint
        )

        hint_words = [
            part
            for part in re.split(
                r"\s+",
                hint,
            )
            if len(part) >= 4
        ][:8]

        best: Locator | None = None

        for group in candidates:
            try:
                count = await group.count()
            except Exception:
                continue

            for index in range(
                max(0, count - 30),
                count,
            ):
                item = group.nth(index)

                try:
                    if not await item.is_visible():
                        continue

                    text = " ".join(
                        (
                            await item.inner_text()
                        ).split()
                    )

                    if not text:
                        continue

                    score = sum(
                        1
                        for word in hint_words
                        if word.lower()
                        in text.lower()
                    )

                    if score >= max(
                        2,
                        min(4, len(hint_words)),
                    ):
                        best = item
                except Exception:
                    continue

        return best

    async def _capture_ref(
        self,
        page: Page,
        caption: str,
    ) -> WebPostRef:
        hint = self._hint_from_caption(
            caption
        )

        item = await self._find_post_element(
            page,
            ref=None,
            caption_hint=hint,
        )

        if item is None:
            return WebPostRef(
                text_hint=hint,
                created_at=time.time(),
            )

        for attr in (
            "data-mid",
            "data-message-id",
            "data-id",
            "id",
        ):
            try:
                value = await item.get_attribute(
                    attr
                )
            except Exception:
                value = None

            if value:
                return WebPostRef(
                    dom_attr=attr,
                    dom_value=value,
                    text_hint=hint,
                    created_at=time.time(),
                )

        return WebPostRef(
            text_hint=hint,
            created_at=time.time(),
        )

    async def send_media_post(
        self,
        image_bytes: bytes,
        caption: str,
    ) -> WebPostRef:
        if not image_bytes:
            raise RuntimeError(
                "Пустое изображение"
            )

        if not caption.strip():
            raise RuntimeError(
                "Пустой caption"
            )

        if len(caption) > 4096:
            raise RuntimeError(
                f"Caption превышает 4096 символов: {len(caption)}"
            )

        async with self._lock:
            _, page = await self._ensure_browser()

            try:
                await self._goto_channel(
                    page
                )
                await self._select_photo_input(
                    page,
                    image_bytes,
                )
                await page.wait_for_timeout(
                    800
                )
                await self._send_media_popup(
                    page,
                    caption,
                )

                ref = await self._capture_ref(
                    page,
                    caption,
                )

                log.info(
                    "Telegram Web: long media post опубликован; "
                    "ref=%s, caption=%s chars",
                    ref.serialize(),
                    len(caption),
                )

                return ref

            except Exception:
                await self._debug_capture(
                    page,
                    "send_media_post_error",
                )
                raise

    async def delete_post(
        self,
        ref: WebPostRef,
        caption_hint: str,
    ) -> None:
        async with self._lock:
            _, page = await self._ensure_browser()

            try:
                await self._goto_channel(
                    page
                )

                item = await self._find_post_element(
                    page,
                    ref=ref,
                    caption_hint=caption_hint,
                )

                if item is None:
                    await self._debug_capture(
                        page,
                        "delete_target_not_found",
                    )
                    raise RuntimeError(
                        "Не найден long-post для удаления. "
                        "Он не будет заменён короткой версией автоматически."
                    )

                await item.scroll_into_view_if_needed()
                await item.click(
                    button="right"
                )
                await page.wait_for_timeout(
                    450
                )

                delete_item = await self._first_visible(
                    [
                        page.get_by_text(
                            re.compile(
                                r"^delete$|^удалить$",
                                re.I,
                            )
                        ),
                        page.get_by_role(
                            "menuitem",
                            name=re.compile(
                                r"delete|удалить",
                                re.I,
                            ),
                        ),
                    ]
                )

                if delete_item is None:
                    await self._debug_capture(
                        page,
                        "delete_menu_not_found",
                    )
                    raise RuntimeError(
                        "В контекстном меню Telegram Web "
                        "не найден пункт Delete."
                    )

                await delete_item.click()
                await page.wait_for_timeout(
                    450
                )

                confirm = await self._first_visible(
                    [
                        page.get_by_role(
                            "button",
                            name=re.compile(
                                r"delete|удалить",
                                re.I,
                            ),
                        ),
                        page.get_by_text(
                            re.compile(
                                r"^delete$|^удалить$",
                                re.I,
                            )
                        ),
                    ]
                )

                if confirm is not None:
                    await confirm.click()

                await page.wait_for_timeout(
                    1400
                )

                log.info(
                    "Telegram Web: long-post удалён; ref=%s",
                    ref.serialize(),
                )

            except Exception:
                await self._debug_capture(
                    page,
                    "delete_post_error",
                )
                raise
