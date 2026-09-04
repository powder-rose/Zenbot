from __future__ import annotations

import html

import asyncio
import base64
import json
import tempfile
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

        # Telegram Web — SPA.
        # Канал может открыться раньше, чем composer станет видимым.
        composer = None

        for _ in range(12):
            composer = await self._get_main_composer(
                page,
                optional=True,
            )

            if composer is not None:
                log.info(
                    "Telegram Web: канал открыт через direct route, "
                    "composer найден."
                )
                return

            await page.wait_for_timeout(500)

        # Fallback через поиск.
        #
        # Telegram Web иногда сначала показывает shimmer/loading,
        # а поле поиска становится видимым через несколько секунд.
        # Поэтому не проверяем его только один раз.
        search = None

        search_candidates = [
            page.locator(
                '.input-search-input'
            ),
            page.locator(
                'input[placeholder*="Search" i]'
            ),
            page.locator(
                'input[placeholder*="Поиск" i]'
            ),
        ]

        for attempt in range(15):
            search = await self._first_visible(
                search_candidates
            )

            if search is not None:
                break

            await page.wait_for_timeout(1000)

        if search is None:
            await self._debug_capture(
                page,
                "channel_open_no_search",
            )
            raise RuntimeError(
                "Не удалось открыть канал в Telegram Web: "
                "поле поиска не стало доступно за 15 секунд."
            )

        await search.click()
        await search.fill(
            self.channel
        )
        await page.wait_for_timeout(1800)

        # Поиск Telegram может сам открыть уже существующий
        # подходящий диалог. В этом случае не нужно искать
        # username в видимом названии результата.
        for _ in range(10):
            composer = await self._get_main_composer(
                page,
                optional=True,
            )

            if composer is not None:
                log.info(
                    "Telegram Web: composer появился после поиска, "
                    "канал уже открыт."
                )
                return

            await page.wait_for_timeout(500)

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
        Ищем окно/слой предпросмотра медиа.

        В текущем Telegram Web K composer выглядит как:
          .new-message-wrapper
          attach-menu-button.attach-file
          .input-message-input
          input[type=file]

        Классы самого media-preview меняются, поэтому не полагаемся
        только на старые popup-send-photo / popup-send-media.
        """
        candidates = [
            page.locator(".popup-new-media"),
            page.locator(".popup-send-photo"),
            page.locator(".popup-send-media"),
            page.locator(".media-editor"),
            page.locator('[class*="media-editor" i]'),
            page.locator('[class*="send-media" i]'),
            page.locator('[class*="new-media" i]'),
            page.locator('[role="dialog"]'),
            page.locator(".popup"),
            page.locator('[class*="popup" i]'),
        ]

        for group in candidates:
            try:
                count = await group.count()
            except Exception:
                continue

            for index in range(min(count, 20)):
                item = group.nth(index)

                try:
                    if not await item.is_visible():
                        continue

                    editable = item.locator(
                        '[contenteditable="true"]'
                    )
                    media = item.locator(
                        "img, video, canvas, "
                        ".media-photo, .media-container, "
                        ".attachment, [class*='media' i]"
                    )

                    if (
                        await editable.count() > 0
                        and await media.count() > 0
                    ):
                        return item
                except Exception:
                    continue

        # Fallback для новой верстки:
        # media-preview может быть не role=dialog и не .popup.
        # Ищем видимый contenteditable, который НЕ является обычным
        # Broadcast composer, и поднимаемся к ближайшему контейнеру с media.
        editors = page.locator(
            '[contenteditable="true"]'
        )

        try:
            count = await editors.count()
        except Exception:
            count = 0

        for index in range(min(count, 30)):
            editor = editors.nth(index)

            try:
                if not await editor.is_visible():
                    continue

                classes = (
                    await editor.get_attribute("class")
                    or ""
                )

                # Пропускаем основной и fake composer канала.
                if "input-message-input" in classes:
                    wrapper = editor.locator(
                        "xpath=ancestor::*[contains(@class,'new-message-wrapper')][1]"
                    )
                    if await wrapper.count():
                        continue

                container = editor.locator(
                    "xpath=ancestor::*["
                    ".//img or .//video or .//canvas or "
                    ".//*[contains(@class,'media')]"
                    "][1]"
                )

                if await container.count():
                    candidate = container.first
                    if await candidate.is_visible():
                        return candidate
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
            int(timeout_ms / 250),
        )

        for _ in range(loops):
            dialog = await self._find_media_dialog(
                page
            )

            if dialog is not None:
                return dialog

            await page.wait_for_timeout(
                250
            )

        raise RuntimeError(
            "media-preview не появился"
        )


    async def _set_exact_composer_file_input(
        self,
        page: Page,
        image_bytes: bytes,
    ) -> bool:
        """
        Используем фактический DOM, полученный из debug HTML пользователя:

        <div class="new-message-wrapper ...">
          <attach-menu-button class="... attach-file ...">
          ...
          <input type="file" multiple style="display:none">
        </div>

        Передаём реальный временный JPEG-файл, а не buffer-object.
        Это ближе к обычному выбору файла пользователем.
        """
        file_input = page.locator(
            ".new-message-wrapper input[type='file']"
        ).first

        if await file_input.count() == 0:
            return False

        temp_path: str | None = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".jpg",
                delete=False,
            ) as tmp:
                tmp.write(image_bytes)
                tmp.flush()
                temp_path = tmp.name

            await file_input.set_input_files(
                temp_path,
                timeout=5000,
            )

            # Telegram Web подписан на input/change.
            # Playwright обычно посылает их сам, но повторяем явно,
            # чтобы новая версия Web K точно увидела изменение FileList.
            try:
                await file_input.dispatch_event(
                    "input"
                )
                await file_input.dispatch_event(
                    "change"
                )
            except Exception:
                pass

            return True

        except Exception:
            log.exception(
                "Telegram Web: не удалось загрузить фото "
                "через точный composer input[type=file]"
            )
            return False

        finally:
            if temp_path:
                try:
                    os.unlink(
                        temp_path
                    )
                except OSError:
                    pass

    async def _dispatch_media_paste(
        self,
        page: Page,
        image_bytes: bytes,
    ) -> bool:
        """
        Fallback №2: имитируем вставку изображения в composer.

        Telegram Web K официально поддерживает paste media.
        Здесь File передаётся через ClipboardEvent/DataTransfer.
        """
        composer = page.locator(
            ".new-message-wrapper "
            ".input-message-input[contenteditable='true']:not(.input-field-input-fake)"
        ).first

        if await composer.count() == 0:
            composer = await self._get_main_composer(
                page,
                optional=True,
            )

        if composer is None or await composer.count() == 0:
            return False

        b64 = base64.b64encode(
            image_bytes
        ).decode("ascii")

        try:
            await composer.focus()

            result = await composer.evaluate(
                """(el, payload) => {
                    try {
                        const binary = atob(payload.b64);
                        const bytes = new Uint8Array(binary.length);

                        for (let i = 0; i < binary.length; i++) {
                            bytes[i] = binary.charCodeAt(i);
                        }

                        const file = new File(
                            [bytes],
                            "article.jpg",
                            {type: "image/jpeg"}
                        );

                        const dt = new DataTransfer();
                        dt.items.add(file);

                        let event;

                        try {
                            event = new ClipboardEvent("paste", {
                                bubbles: true,
                                cancelable: true,
                                clipboardData: dt
                            });
                        } catch (_) {
                            event = new Event("paste", {
                                bubbles: true,
                                cancelable: true
                            });
                        }

                        try {
                            Object.defineProperty(
                                event,
                                "clipboardData",
                                {value: dt}
                            );
                        } catch (_) {}

                        el.dispatchEvent(event);
                        return true;
                    } catch (e) {
                        return String(e);
                    }
                }""",
                {
                    "b64": b64,
                },
            )

            if result is True:
                log.info(
                    "Telegram Web: отправлен synthetic paste "
                    "с image/jpeg."
                )
                return True

            log.warning(
                "Telegram Web synthetic paste result=%r",
                result,
            )
            return False

        except Exception:
            log.exception(
                "Telegram Web: synthetic paste изображения не удался"
            )
            return False

    async def _dispatch_media_drop(
        self,
        page: Page,
        image_bytes: bytes,
    ) -> bool:
        """
        Fallback №3: имитируем drag-and-drop файла на текущий чат.
        Telegram Web K поддерживает drag-and-drop media.
        """
        b64 = base64.b64encode(
            image_bytes
        ).decode("ascii")

        target = page.locator(
            "#column-center, .bubbles, .chat-input"
        ).last

        if await target.count() == 0:
            return False

        try:
            result = await target.evaluate(
                """(el, payload) => {
                    try {
                        const binary = atob(payload.b64);
                        const bytes = new Uint8Array(binary.length);

                        for (let i = 0; i < binary.length; i++) {
                            bytes[i] = binary.charCodeAt(i);
                        }

                        const file = new File(
                            [bytes],
                            "article.jpg",
                            {type: "image/jpeg"}
                        );

                        const dt = new DataTransfer();
                        dt.items.add(file);

                        for (const type of [
                            "dragenter",
                            "dragover",
                            "drop"
                        ]) {
                            const event = new DragEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                dataTransfer: dt
                            });

                            el.dispatchEvent(event);
                        }

                        return true;
                    } catch (e) {
                        return String(e);
                    }
                }""",
                {
                    "b64": b64,
                },
            )

            if result is True:
                log.info(
                    "Telegram Web: отправлен synthetic drag-and-drop "
                    "с image/jpeg."
                )
                return True

            log.warning(
                "Telegram Web synthetic drop result=%r",
                result,
            )
            return False

        except Exception:
            log.exception(
                "Telegram Web: synthetic drag-and-drop изображения "
                "не удался"
            )
            return False

    async def _select_photo_input(
        self,
        page: Page,
        image_bytes: bytes,
    ) -> Locator:
        """
        v46: загрузка построена по фактическому DOM Telegram Web K
        из присланного debug HTML.

        Порядок:
        1. точный hidden input внутри .new-message-wrapper;
        2. synthetic paste изображения;
        3. synthetic drag-and-drop.

        После каждого способа ОБЯЗАТЕЛЬНО проверяем media-preview.
        Если ни один путь не открыл preview, ничего не публикуем.
        """
        methods = [
            (
                "exact_file_input",
                self._set_exact_composer_file_input,
            ),
            (
                "paste",
                self._dispatch_media_paste,
            ),
            (
                "drag_drop",
                self._dispatch_media_drop,
            ),
        ]

        for method_name, method in methods:
            log.info(
                "Telegram Web: пробую прикрепить фото методом %s",
                method_name,
            )

            ok = await method(
                page,
                image_bytes,
            )

            if not ok:
                continue

            try:
                dialog = await self._wait_for_media_dialog(
                    page,
                    timeout_ms=4500,
                )

                log.info(
                    "Telegram Web: фотография прикреплена "
                    "методом %s, media-preview открыт.",
                    method_name,
                )

                return dialog

            except RuntimeError:
                # Не считаем окончательной ошибкой до прохождения
                # остальных методов.
                log.warning(
                    "Telegram Web: метод %s не открыл media-preview.",
                    method_name,
                )

                # Убираем возможный file selection перед следующей попыткой.
                try:
                    file_input = page.locator(
                        ".new-message-wrapper input[type='file']"
                    ).first
                    if await file_input.count():
                        await file_input.set_input_files(
                            []
                        )
                except Exception:
                    pass

                await page.wait_for_timeout(
                    500
                )

        await self._debug_capture(
            page,
            "all_media_attach_methods_failed",
        )

        raise RuntimeError(
            "Telegram Web не прикрепил изображение ни через "
            "composer file input, ни через paste, ни через drag-and-drop. "
            "Публикация остановлена; debug сохранён."
        )

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

    async def _media_send_completed(
        self,
        page: Page,
        dialog: Locator,
        caption: str,
        *,
        timeout_ms: int = 7000,
    ) -> bool:
        """
        После Send проверяем фактический результат:
        - media-preview закрылся, либо
        - в чате появился исходящий пост по началу caption.

        Это позволяет безопасно использовать keyboard fallback,
        не отправляя голый текст из обычного composer.
        """
        loops = max(
            1,
            int(timeout_ms / 350),
        )

        hint = self._hint_from_caption(
            caption
        )

        for _ in range(loops):
            try:
                if not await dialog.is_visible():
                    return True
            except Exception:
                # DOM preview уничтожен после отправки.
                return True

            try:
                post = await self._find_post_element(
                    page,
                    ref=None,
                    caption_hint=hint,
                )

                if post is not None:
                    media = post.locator(
                        ".attachment img, "
                        ".attachment video, "
                        ".media-photo, "
                        ".media-container img, "
                        ".media-container video"
                    )

                    if await media.count() > 0:
                        return True
            except Exception:
                pass

            await page.wait_for_timeout(
                350
            )

        return False

    async def _find_media_send_button(
        self,
        page: Page,
        dialog: Locator,
    ) -> Locator | None:
        """
        Telegram Web K меняет DOM media preview между версиями.

        Ищем Send:
        1. внутри найденного media-preview;
        2. в видимом popup/media layer;
        3. глобально, но НИКОГДА не кликаем обычную кнопку записи
           composer с классом `record`.
        """
        candidates = [
            dialog.locator(
                "button.btn-send:not(.record)"
            ),
            dialog.locator(
                ".btn-send:not(.record)"
            ),
            dialog.locator(
                'button[aria-label="Send"]'
            ),
            dialog.locator(
                'button[aria-label="Отправить"]'
            ),
            dialog.get_by_role(
                "button",
                name=re.compile(
                    r"^send$|^отправить$",
                    re.I,
                ),
            ),

            page.locator(
                ".popup:not(.hide) button.btn-send:not(.record)"
            ),
            page.locator(
                "[class*='media' i] button.btn-send:not(.record)"
            ),
            page.locator(
                "[role='dialog'] button.btn-send:not(.record)"
            ),
            page.locator(
                "button.btn-send:not(.record)"
            ),
            page.get_by_role(
                "button",
                name=re.compile(
                    r"^send$|^отправить$",
                    re.I,
                ),
            ),
        ]

        send = await self._first_visible(
            candidates
        )

        if send is not None:
            return send

        # Иногда надпись Send находится во внутреннем span/div,
        # а pointer events принимает родитель.
        label = await self._first_visible(
            [
                dialog.get_by_text(
                    re.compile(
                        r"^send$|^отправить$",
                        re.I,
                    )
                ),
                page.get_by_text(
                    re.compile(
                        r"^send$|^отправить$",
                        re.I,
                    )
                ),
            ]
        )

        if label is None:
            return None

        wrapper = label.locator(
            "xpath=ancestor::*["
            "self::button or "
            "contains(@class,'btn-send') or "
            "contains(@class,'btn-primary')"
            "][1]"
        )

        if await wrapper.count():
            return wrapper

        return None

    @staticmethod
    def _caption_markdown_to_html(
        caption: str,
    ) -> tuple[str, str]:
        """
        Преобразует простую разметку пользователя
        в HTML для Telegram Web.

        Поддерживается:
        **жирный**
        *курсив*
        __подчёркнутый__
        ~~зачёркнутый~~
        ==выделенный==

        ==...== отображаем жирным, так как отдельного
        текстового маркера-highlight Telegram не имеет.
        """
        source = str(caption or "")

        # ----------------------------------------------------
        # Пользовательский prompt может использовать
        # собственные теги форматирования.
        # Сначала приводим их к единому Markdown-виду.
        # ----------------------------------------------------

        custom_replacements = {
            "[[B]]": "**",
            "[[/B]]": "**",

            "[[I]]": "*",
            "[[/I]]": "*",

            "[[U]]": "__",
            "[[/U]]": "__",

            "[[S]]": "~~",
            "[[/S]]": "~~",
        }

        for old, new in custom_replacements.items():
            source = source.replace(
                old,
                new,
            )

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

        source = re.sub(
            r"\[\[Q\]\](.*?)\[\[/Q\]\]",
            quote_repl,
            source,
            flags=re.I | re.S,
        )

        # [URL](URL) -> обычный URL.
        source = re.sub(
            r"\[(https?://[^\]\s]+)\]\(\1\)",
            r"\1",
            source,
        )

        def render_inline(value: str) -> str:
            value = html.escape(
                value,
                quote=False,
            )

            value = re.sub(
                r"\*\*(.+?)\*\*",
                r"<b>\1</b>",
                value,
            )

            value = re.sub(
                r"__(.+?)__",
                r"<u>\1</u>",
                value,
            )

            value = re.sub(
                r"~~(.+?)~~",
                r"<s>\1</s>",
                value,
            )

            value = re.sub(
                r"==(.+?)==",
                r"<b>\1</b>",
                value,
            )

            value = re.sub(
                r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
                r"<i>\1</i>",
                value,
            )

            value = re.sub(
                r"(?<!_)_([^_\n]+?)_(?!_)",
                r"<i>\1</i>",
                value,
            )

            return value

        lines = source.split("\n")

        rendered_lines: list[str] = []

        for index, line in enumerate(lines):
            stripped = line.lstrip()

            is_quote = (
                stripped.startswith("> ")
                or stripped == ">"
            )

            if is_quote:
                quote_text = (
                    stripped[1:].lstrip()
                )

                rendered = render_inline(
                    quote_text
                )

                rendered = (
                    f"<blockquote>{rendered}</blockquote>"
                )
            else:
                rendered = render_inline(
                    line
                )

            # Первая строка caption — заголовок.
            if (
                index == 0
                and rendered.strip()
                and not is_quote
                and not rendered.lstrip().startswith("<b>")
            ):
                rendered = f"<b>{rendered}</b>"

            rendered_lines.append(
                rendered
            )

        rich_html = "<br>".join(
            rendered_lines
        )

        plain = source

        replacements = [
            (r"\*\*(.+?)\*\*", r"\1"),
            (r"__(.+?)__", r"\1"),
            (r"~~(.+?)~~", r"\1"),
            (r"==(.+?)==", r"\1"),
            (
                r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
                r"\1",
            ),
            (
                r"(?<!_)_([^_\n]+?)_(?!_)",
                r"\1",
            ),
        ]

        for pattern, repl in replacements:
            plain = re.sub(
                pattern,
                repl,
                plain,
            )

        return plain, rich_html


    async def _fill_rich_caption(
        self,
        page: Page,
        editor: Locator,
        caption: str,
    ) -> str:
        """
        Вставляет LONG caption в Telegram Web
        с реальным форматированием.

        Возвращает plain-caption без Markdown-маркеров,
        чтобы последующая проверка отправки искала
        фактически опубликованный текст.
        """
        plain, rich_html = (
            self._caption_markdown_to_html(
                caption
            )
        )

        await editor.click(
            force=True
        )

        # Сначала очищаем contenteditable штатным способом.
        await editor.fill("")

        try:
            result = await editor.evaluate(
                """(el, richHtml) => {
                    try {
                        el.focus();

                        const selection =
                            window.getSelection();

                        const range =
                            document.createRange();

                        range.selectNodeContents(el);
                        range.collapse(false);

                        selection.removeAllRanges();
                        selection.addRange(range);

                        const ok =
                            document.execCommand(
                                "insertHTML",
                                false,
                                richHtml
                            );

                        el.dispatchEvent(
                            new InputEvent(
                                "input",
                                {
                                    bubbles: true,
                                    inputType: "insertHTML"
                                }
                            )
                        );

                        return {
                            ok: Boolean(ok),
                            text: el.innerText || "",
                            html: el.innerHTML || ""
                        };
                    } catch (e) {
                        return {
                            ok: false,
                            error: String(e)
                        };
                    }
                }""",
                rich_html,
            )

            if (
                isinstance(result, dict)
                and result.get("ok")
            ):
                log.info(
                    "Telegram Web: LONG caption "
                    "вставлен с rich formatting."
                )

                await page.wait_for_timeout(
                    300
                )

                return plain

            log.warning(
                "Telegram Web: rich caption "
                "insertHTML не подтверждён: %r",
                result,
            )

        except Exception:
            log.exception(
                "Telegram Web: rich caption "
                "вставить не удалось."
            )

        # --------------------------------------------
        # Безопасный fallback:
        # если rich-вставка не поддержалась,
        # отправляем хотя бы чистый текст
        # БЕЗ Markdown-звёздочек.
        # --------------------------------------------

        await editor.fill(
            plain
        )

        # Заголовок остаётся жирным,
        # как работало раньше.
        try:
            await editor.press(
                "Control+Home"
            )
            await editor.press(
                "Shift+End"
            )
            await editor.press(
                "Control+b"
            )
            await editor.press(
                "End"
            )
        except Exception:
            log.exception(
                "Не удалось применить жирное "
                "начертание к LONG-заголовку."
            )

        await page.wait_for_timeout(
            300
        )

        return plain


    async def _send_media_popup(
        self,
        page: Page,
        dialog: Locator,
        caption: str,
        *,
        silent: bool = False,
    ) -> None:
        editor = await self._get_caption_editor(
            page,
            dialog,
        )

        # Markdown из пользовательского LONG-промпта
        # превращаем в настоящее форматирование Telegram Web.
        #
        # caption заменяем на plain-версию, чтобы дальнейшая
        # проверка фактически отправленного сообщения
        # не искала Markdown-маркеры.
        caption = await self._fill_rich_caption(
            page,
            editor,
            caption,
        )

        send = await self._find_media_send_button(
            page,
            dialog,
        )

        if silent:
            if send is None:
                await self._debug_capture(
                    page,
                    "silent_send_button_not_found",
                )
                raise RuntimeError(
                    "Telegram Web: запрошена тихая отправка, "
                    "но кнопка Send не найдена. "
                    "Обычная отправка запрещена."
                )

            silent_pattern = re.compile(
                (
                    r"send\s+without\s+sound|"
                    r"send\s+silently|"
                    r"without\s+sound|"
                    r"отправить\s+без\s+звука|"
                    r"без\s+звука"
                ),
                re.I,
            )

            async def find_silent_action():
                return await self._first_visible(
                    [
                        page.get_by_role(
                            "menuitem",
                            name=silent_pattern,
                        ),
                        page.get_by_role(
                            "button",
                            name=silent_pattern,
                        ),
                        page.get_by_text(
                            silent_pattern
                        ),
                        page.locator(
                            '[role="menuitem"], '
                            '.btn-menu-item, '
                            '.menu-item, '
                            '[class*="MenuItem"], '
                            '[class*="menu-item"]'
                        ).filter(
                            has_text=silent_pattern
                        ),
                    ]
                )

            silent_item = None

            # Telegram Web обычно открывает дополнительные
            # варианты отправки по правому клику Send.
            try:
                await send.click(
                    button="right",
                    force=True,
                    timeout=5000,
                )
                await page.wait_for_timeout(
                    700
                )
                silent_item = await find_silent_action()
            except Exception:
                silent_item = None

            # Если правый клик открыл меню, но нужного
            # пункта там нет, закрываем его перед long-press.
            if silent_item is None:
                try:
                    await page.keyboard.press(
                        "Escape"
                    )
                    await page.wait_for_timeout(
                        300
                    )
                except Exception:
                    pass

            # Fallback: длительное нажатие на Send.
            if silent_item is None:
                try:
                    box = await send.bounding_box()

                    if box:
                        x = (
                            box["x"]
                            + box["width"] / 2
                        )
                        y = (
                            box["y"]
                            + box["height"] / 2
                        )

                        await page.mouse.move(
                            x,
                            y,
                        )
                        await page.mouse.down()
                        await page.wait_for_timeout(
                            900
                        )
                        await page.mouse.up()

                        await page.wait_for_timeout(
                            700
                        )

                        silent_item = (
                            await find_silent_action()
                        )
                except Exception:
                    silent_item = None

            if silent_item is None:
                await self._debug_capture(
                    page,
                    "silent_send_menu_not_found",
                )
                raise RuntimeError(
                    "Telegram Web: не найден пункт "
                    "'Send without sound / Отправить без звука'. "
                    "Обычная отправка LONG запрещена."
                )

            wrapper = silent_item.locator(
                "xpath=ancestor-or-self::*["
                "self::button or "
                "@role='menuitem' or "
                "contains(@class,'btn-menu-item') or "
                "contains(@class,'menu-item')"
                "][1]"
            )

            if await wrapper.count():
                await wrapper.click(
                    force=True,
                    timeout=5000,
                )
            else:
                await silent_item.click(
                    force=True,
                    timeout=5000,
                )

            if await self._media_send_completed(
                page,
                dialog,
                caption,
                timeout_ms=7000,
            ):
                log.info(
                    "Telegram Web: media post отправлен "
                    "БЕЗ ЗВУКА."
                )
                return

            await self._debug_capture(
                page,
                "silent_send_not_completed",
            )

            raise RuntimeError(
                "Telegram Web: выбран тихий режим, "
                "но media post не был подтверждён как отправленный."
            )

        if send is not None:
            try:
                await send.click(
                    force=True,
                    timeout=5000,
                )

                if await self._media_send_completed(
                    page,
                    dialog,
                    caption,
                    timeout_ms=7000,
                ):
                    log.info(
                        "Telegram Web: media post отправлен "
                        "через найденную кнопку Send."
                    )
                    return

                log.warning(
                    "Telegram Web: клик по Send выполнен, "
                    "но media-preview не закрылся. "
                    "Перехожу к keyboard fallback."
                )

            except Exception:
                log.exception(
                    "Telegram Web: найденная кнопка Send "
                    "не сработала. Пробую keyboard fallback."
                )
        else:
            log.warning(
                "Telegram Web: отдельная кнопка Send "
                "в media-preview не найдена. "
                "Пробую keyboard fallback."
            )

        # В Telegram Web обычное поведение media caption:
        # Enter отправляет, Shift+Enter добавляет перенос.
        # После нажатия ОБЯЗАТЕЛЬНО проверяем, что preview закрылся.
        try:
            await editor.focus()
            await page.keyboard.press(
                "Enter"
            )

            if await self._media_send_completed(
                page,
                dialog,
                caption,
                timeout_ms=5000,
            ):
                log.info(
                    "Telegram Web: media post отправлен "
                    "keyboard fallback Enter."
                )
                return

            log.warning(
                "Telegram Web: Enter не отправил media post."
            )
        except Exception:
            log.exception(
                "Telegram Web: fallback Enter завершился ошибкой."
            )

        # Если в настройках Telegram Enter создаёт перенос,
        # пробуем Ctrl+Enter. Если предыдущий Enter добавил новую строку,
        # это не критично для caption.
        try:
            await editor.focus()
            await page.keyboard.press(
                "Control+Enter"
            )

            if await self._media_send_completed(
                page,
                dialog,
                caption,
                timeout_ms=5000,
            ):
                log.info(
                    "Telegram Web: media post отправлен "
                    "keyboard fallback Ctrl+Enter."
                )
                return

        except Exception:
            log.exception(
                "Telegram Web: fallback Ctrl+Enter завершился ошибкой."
            )

        await self._debug_capture(
            page,
            "media_send_failed",
        )

        raise RuntimeError(
            "Фотография прикреплена и media-preview открыт, "
            "но Telegram Web не выполнил отправку ни кнопкой Send, "
            "ни Enter, ни Ctrl+Enter. Debug сохранён."
        )

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
        *,
        silent: bool = False,
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
                    silent=silent,
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
                await item.hover()
                await item.click(button="right")
                await page.wait_for_timeout(700)

                delete_pattern = re.compile(
                    r"delete(?:\s+(?:message|post))?|remove(?:\s+(?:message|post))?|"
                    r"удалить(?:\s+(?:сообщение|публикацию|пост))?",
                    re.I,
                )

                async def find_delete_action():
                    # Telegram Web меняет разметку контекстного меню. Ищем не только
                    # точный span Delete, но и пункты вроде Delete message / Удалить сообщение.
                    return await self._first_visible(
                        [
                            page.get_by_role("menuitem", name=delete_pattern),
                            page.get_by_role("button", name=delete_pattern),
                            page.get_by_text(delete_pattern),
                            page.locator(
                                '[data-action*="delete" i], '
                                '[data-testid*="delete" i], '
                                '[aria-label*="delete" i], '
                                '[title*="delete" i], '
                                '[class*="menu-item"]'
                            ).filter(has_text=delete_pattern),
                            page.locator(
                                '.btn-menu-item, .menu-item, [role="menuitem"], '
                                '[class*="MenuItem"], [class*="context-menu"] *'
                            ).filter(has_text=delete_pattern),
                        ]
                    )

                delete_item = await find_delete_action()

                # В некоторых сборках Telegram Web правый клик не открывает меню,
                # зато после hover появляется кнопка «ещё» / menu toggle.
                if delete_item is None:
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass

                    await item.hover()
                    more_candidates = [
                        item.locator(
                            'button[aria-label*="more" i], '
                            'button[title*="more" i], '
                            '[aria-label*="menu" i], '
                            '[title*="menu" i], '
                            '[data-testid*="menu" i], '
                            '[class*="menu-toggle"], '
                            '[class*="more"]'
                        ),
                        item.locator("xpath=..").locator(
                            'button[aria-label*="more" i], '
                            'button[title*="more" i], '
                            '[aria-label*="menu" i], '
                            '[title*="menu" i], '
                            '[data-testid*="menu" i], '
                            '[class*="menu-toggle"]'
                        ),
                    ]

                    more_button = await self._first_visible(more_candidates)
                    if more_button is not None:
                        try:
                            await more_button.click(force=True, timeout=4000)
                            await page.wait_for_timeout(700)
                            delete_item = await find_delete_action()
                        except Exception:
                            pass

                # Последний DOM-fallback: среди видимых коротких элементов меню
                # кликаем тот, в тексте/aria-label/title которого явно есть delete.
                if delete_item is None:
                    clicked = await page.evaluate(
                        r"""() => {
                            const rx = /(delete(?:\s+(?:message|post))?|remove(?:\s+(?:message|post))?|удалить(?:\s+(?:сообщение|публикацию|пост))?)/i;
                            const visible = (el) => {
                                const r = el.getBoundingClientRect();
                                const s = getComputedStyle(el);
                                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                            };
                            const els = Array.from(document.querySelectorAll(
                                '[role=menuitem], button, .btn-menu-item, .menu-item, [class*=MenuItem], [class*=menu-item], [data-action], [data-testid], [aria-label], [title]'
                            ));
                            for (const el of els) {
                                if (!visible(el)) continue;
                                const label = [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-action'), el.getAttribute('data-testid')]
                                    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
                                if (!label || label.length > 180 || !rx.test(label)) continue;
                                const clickable = el.closest('button,[role=menuitem],.btn-menu-item,.menu-item,[class*=MenuItem],[class*=menu-item]') || el;
                                clickable.click();
                                return label;
                            }
                            return null;
                        }"""
                    )
                    if clicked:
                        log.info(
                            "Telegram Web: Delete выбран через DOM fallback: %s",
                            clicked,
                        )
                        await page.wait_for_timeout(700)
                    else:
                        await self._debug_capture(
                            page,
                            "delete_menu_not_found",
                        )
                        raise RuntimeError(
                            "В контекстном меню Telegram Web не найден пункт Delete/Удалить."
                        )
                else:
                    # Кликаем по ближайшему интерактивному wrapper, а не по вложенному span.
                    menu_wrapper = delete_item.locator(
                        "xpath=ancestor-or-self::*[self::button or @role='menuitem' "
                        "or contains(@class,'btn-menu-item') or contains(@class,'menu-item')][1]"
                    )
                    if await menu_wrapper.count():
                        await menu_wrapper.click(force=True, timeout=5000)
                    else:
                        await delete_item.click(force=True, timeout=5000)
                    await page.wait_for_timeout(700)

                # После выбора Delete Telegram может удалить сразу либо показать confirm.
                confirm = await self._first_visible(
                    [
                        page.get_by_role("button", name=delete_pattern),
                        page.get_by_role("menuitem", name=delete_pattern),
                        page.get_by_text(delete_pattern),
                        page.locator(
                            'button[class*="danger"], [class*="modal"] button, '
                            '[class*="popup"] button, [class*="confirm"] button'
                        ).filter(has_text=delete_pattern),
                    ]
                )

                if confirm is not None:
                    confirm_wrapper = confirm.locator(
                        "xpath=ancestor-or-self::*[self::button or @role='button' "
                        "or contains(@class,'danger') or contains(@class,'confirm')][1]"
                    )
                    if await confirm_wrapper.count():
                        await confirm_wrapper.click(force=True, timeout=5000)
                    else:
                        await confirm.click(force=True, timeout=5000)
                    await page.wait_for_timeout(1200)
                else:
                    # Некоторые версии удаляют без второго подтверждения. Проверяем,
                    # исчез ли именно наш long-post, прежде чем считать это ошибкой.
                    await page.wait_for_timeout(900)
                    still_there = await self._find_post_element(
                        page,
                        ref=ref,
                        caption_hint=caption_hint,
                    )
                    if still_there is not None:
                        await self._debug_capture(
                            page,
                            "delete_confirm_not_found",
                        )
                        raise RuntimeError(
                            "Delete выбран, но long-post остался и подтверждение удаления не найдено."
                        )

                await page.wait_for_timeout(500)

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
