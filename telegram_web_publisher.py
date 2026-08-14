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
        """
        Проверяем авторизацию только после того, как Telegram Web
        реально открыт.

        ВАЖНО:
        generic <canvas> больше НЕ используется как признак login-page,
        потому что Telegram Web может использовать canvas и внутри
        уже авторизованного интерфейса.
        """
        if "web.telegram.org" not in page.url:
            return False

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
            page.locator(
                '.input-message-input[contenteditable="true"]'
            ),
            page.locator(
                '.message-input [contenteditable="true"]'
            ),
        ]

        marker = await self._first_visible(
            authorized_markers
        )

        if marker is not None:
            return True

        # Только явные элементы формы логина.
        login_markers = [
            page.get_by_text(
                re.compile(
                    r"log in by phone|войти по номеру|"
                    r"log in by qr code|войти по qr",
                    re.I,
                )
            ),
            page.locator(
                'input[type="tel"]'
            ),
            page.locator(
                'input[autocomplete="one-time-code"]'
            ),
        ]

        login_marker = await self._first_visible(
            login_markers
        )

        if login_marker is not None:
            return False

        # Telegram Web — SPA. Во время переходов sidebar/composer
        # иногда кратковременно отсутствуют. Если мы остаёмся на
        # web.telegram.org и явной формы логина нет, считаем сессию
        # авторизованной.
        return True

    async def _require_authorized(
        self,
        page: Page,
    ) -> None:
        # Telegram Web — SPA; после навигации интерфейс может
        # дорисовываться несколько секунд.
        for _ in range(12):
            if await self._is_authorized(
                page
            ):
                return

            await page.wait_for_timeout(
                500
            )

        await self._debug_capture(
            page,
            "not_authorized",
        )

        raise RuntimeError(
            "Telegram Web действительно показывает неавторизованное "
            "состояние после открытия web.telegram.org. "
            "Проверьте persistent profile и data/telegram_web_debug."
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
        """
        ВАЖНО: сначала открываем Telegram Web, и только потом
        проверяем авторизацию.

        Раньше проверка выполнялась на about:blank при первом запуске
        Chromium внутри bot.py. Поэтому healthcheck проходил, а реальная
        публикация ошибочно получала "Telegram Web не авторизован".
        """
        channel_url = (
            f"{self.web_url}#@{self.channel}"
        )

        await page.goto(
            channel_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(3000)

        await self._require_authorized(
            page
        )

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

    async def _find_media_dialog(
        self,
        page: Page,
    ) -> Locator | None:
        """
        Ищем именно окно предпросмотра прикреплённого медиа.

        Главное правило v45: если preview фотографии не появился,
        пост НЕ отправляем как обычный текст.
        """
        candidates = [
            page.locator(
                ".popup-send-photo"
            ),
            page.locator(
                ".popup-send-media"
            ),
            page.locator(
                ".media-editor"
            ),
            page.locator(
                ".popup .media-editor"
            ),
            page.locator(
                '[role="dialog"]'
            ),
            page.locator(
                ".popup"
            ),
        ]

        for group in candidates:
            try:
                count = await group.count()
            except Exception:
                continue

            for index in range(
                min(count, 12)
            ):
                item = group.nth(index)

                try:
                    if not await item.is_visible():
                        continue

                    # Media-preview должен содержать хотя бы редактор caption
                    # и визуальный media-элемент.
                    editable = item.locator(
                        '[contenteditable="true"]'
                    )

                    media = item.locator(
                        "img, video, canvas, "
                        ".media-photo, .media-container, "
                        ".attachment, [class*='media']"
                    )

                    if (
                        await editable.count() > 0
                        and await media.count() > 0
                    ):
                        return item
                except Exception:
                    continue

        return None

    async def _wait_for_media_dialog(
        self,
        page: Page,
        *,
        timeout_ms: int = 12000,
    ) -> Locator:
        loops = max(
            1,
            int(timeout_ms / 300),
        )

        for _ in range(loops):
            dialog = await self._find_media_dialog(
                page
            )

            if dialog is not None:
                return dialog

            await page.wait_for_timeout(
                300
            )

        await self._debug_capture(
            page,
            "media_preview_not_opened",
        )

        raise RuntimeError(
            "Telegram Web не открыл предпросмотр фотографии. "
            "Публикация остановлена, чтобы не отправить статью "
            "как обычный текст без изображения."
        )

    async def _set_photo_file_input(
        self,
        page: Page,
        payload: dict,
    ) -> bool:
        """
        Fallback для версий Telegram Web, где file input уже присутствует
        после открытия меню Attach.
        """
        inputs = page.locator(
            'input[type="file"]'
        )

        try:
            count = await inputs.count()
        except Exception:
            return False

        image_inputs: list[Locator] = []
        other_inputs: list[Locator] = []

        for index in range(count):
            item = inputs.nth(index)

            try:
                accept = (
                    await item.get_attribute(
                        "accept"
                    )
                    or ""
                ).lower()
            except Exception:
                accept = ""

            if (
                "image" in accept
                or "video" in accept
            ):
                image_inputs.append(
                    item
                )
            else:
                other_inputs.append(
                    item
                )

        # Сначала используем только input, явно предназначенный для media.
        for item in (
            image_inputs
            + other_inputs
        ):
            try:
                await item.set_input_files(
                    payload
                )
                return True
            except Exception:
                continue

        return False

    async def _select_photo_input(
        self,
        page: Page,
        image_bytes: bytes,
    ) -> Locator:
        """
        Надёжная загрузка фотографии.

        v45 больше НЕ выбирает случайный скрытый input[type=file] до
        открытия Attach. Сначала открывается Attach -> Photo/Video,
        затем файл передаётся именно в этот chooser/input.

        Возвращает media-preview dialog.
        """
        payload = {
            "name": "article.jpg",
            "mimeType": "image/jpeg",
            "buffer": image_bytes,
        }

        # Сначала открываем Attach.
        attach = await self._first_visible(
            [
                page.locator(
                    'button[aria-label*="Attach" i]'
                ),
                page.locator(
                    'button[aria-label*="Прикреп" i]'
                ),
                page.locator(
                    ".btn-icon.tgico-attach"
                ),
                page.locator(
                    '[data-tippy-content*="Attach" i]'
                ),
                page.locator(
                    '[class*="attach" i]'
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

        try:
            await attach.click(
                force=True,
                timeout=5000,
            )
        except Exception:
            # Иногда pointer-events принимает wrapper.
            wrapper = attach.locator(
                "xpath=ancestor::button[1]"
            )

            if await wrapper.count():
                await wrapper.click(
                    force=True,
                    timeout=5000,
                )
            else:
                raise

        await page.wait_for_timeout(
            500
        )

        photo_menu = await self._first_visible(
            [
                page.get_by_text(
                    re.compile(
                        r"photo|video|фото|видео",
                        re.I,
                    )
                ),
                page.get_by_role(
                    "menuitem",
                    name=re.compile(
                        r"photo|video|фото|видео",
                        re.I,
                    ),
                ),
            ]
        )

        chooser_used = False

        if photo_menu is not None:
            # Кликаем по action-wrapper меню, а не по внутреннему span.i18n.
            menu_wrapper = photo_menu.locator(
                "xpath=ancestor::div["
                "contains(@class,'btn-menu-item') or "
                "contains(@class,'menu-item')"
                "][1]"
            )

            click_target = (
                menu_wrapper
                if await menu_wrapper.count()
                else photo_menu
            )

            try:
                async with page.expect_file_chooser(
                    timeout=5000
                ) as chooser_info:
                    await click_target.click(
                        force=True,
                        timeout=5000,
                    )

                chooser = await chooser_info.value
                await chooser.set_files(
                    payload
                )
                chooser_used = True

            except PlaywrightTimeoutError:
                # В некоторых сборках клик не создаёт filechooser event,
                # а только активирует заранее существующий input.
                chooser_used = False

        if not chooser_used:
            if not await self._set_photo_file_input(
                page,
                payload,
            ):
                await self._debug_capture(
                    page,
                    "photo_input_failed",
                )
                raise RuntimeError(
                    "Telegram Web не принял файл изображения."
                )

        # Критическая проверка: media-preview обязан появиться.
        dialog = await self._wait_for_media_dialog(
            page,
            timeout_ms=12000,
        )

        log.info(
            "Telegram Web: фотография прикреплена, "
            "media-preview открыт."
        )

        return dialog

    async def _get_caption_editor(
        self,
        page: Page,
        dialog: Locator,
    ) -> Locator:
        """
        Caption ищем только внутри media-preview, а не среди всех
        contenteditable на странице. Это исключает отправку текста
        в основной composer без фотографии.
        """
        candidates = dialog.locator(
            '[contenteditable="true"]'
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
                "Media-preview открыт, но поле caption не найдено."
            )

        visible.sort(
            key=lambda pair: pair[0],
            reverse=True,
        )

        return visible[0][1]

    async def _send_media_popup(
        self,
        page: Page,
        dialog: Locator,
        caption: str,
    ) -> None:
        editor = await self._get_caption_editor(
            page,
            dialog,
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
                dialog.locator(
                    'button[aria-label="Send"]'
                ),
                dialog.locator(
                    'button[aria-label="Отправить"]'
                ),
                dialog.locator(
                    'button.btn-send'
                ),
                dialog.get_by_role(
                    "button",
                    name=re.compile(
                        r"^send$|^отправить$",
                        re.I,
                    ),
                ),
                page.locator(
                    '.popup-send-photo .btn-send'
                ),
                page.locator(
                    '.popup-send-media .btn-send'
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

        await send.click(
            force=True,
            timeout=5000,
        )
        await page.wait_for_timeout(2500)

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
                media_dialog = await self._select_photo_input(
                    page,
                    image_bytes,
                )
                await page.wait_for_timeout(
                    500
                )
                await self._send_media_popup(
                    page,
                    media_dialog,
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

                # Telegram Web возвращает текстовый <span class="i18n">Delete</span>,
                # но pointer events принимает родительский div.btn-menu-item.
                # Поэтому кликаем по ближайшему action-wrapper, а не по span.
                menu_wrapper = delete_item.locator(
                    "xpath=ancestor::div[contains(@class,'btn-menu-item')][1]"
                )

                if await menu_wrapper.count():
                    await menu_wrapper.click(
                        force=True,
                        timeout=5000,
                    )
                else:
                    await delete_item.click(
                        force=True,
                        timeout=5000,
                    )

                await page.wait_for_timeout(
                    650
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

                if confirm is None:
                    await self._debug_capture(
                        page,
                        "delete_confirm_not_found",
                    )
                    raise RuntimeError(
                        "После выбора Delete не найдено "
                        "подтверждение удаления."
                    )

                # Подтверждение в разных версиях Telegram Web может быть
                # <button>, div.btn-primary, div.danger и т.п.
                confirm_wrapper = confirm.locator(
                    "xpath=ancestor-or-self::button[1]"
                )

                if not await confirm_wrapper.count():
                    confirm_wrapper = confirm.locator(
                        "xpath=ancestor::div["
                        "contains(@class,'btn') or "
                        "contains(@class,'danger') or "
                        "contains(@class,'confirm')"
                        "][1]"
                    )

                if await confirm_wrapper.count():
                    await confirm_wrapper.click(
                        force=True,
                        timeout=5000,
                    )
                else:
                    await confirm.click(
                        force=True,
                        timeout=5000,
                    )

                await page.wait_for_timeout(
                    1600
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
