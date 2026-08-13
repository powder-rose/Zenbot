from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

log = logging.getLogger(__name__)


class DzenPublishError(RuntimeError):
    pass


def _plain_text(value: str) -> str:
    value = re.sub(r"</?(?:b|i)>", "", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


class DzenPublisher:
    """
    Публикация через сохранённую сессию Playwright.

    Для пустого редактора Дзена приоритетно используется схема:
    первый видимый [contenteditable] = заголовок,
    второй видимый [contenteditable] = тело статьи.
    """

    def __init__(
        self,
        *,
        editor_url: str,
        profile_dir: Path,
        headless: bool = True,
        new_article_selector: str | None = None,
        title_selector: str | None = None,
        body_selector: str | None = None,
        publish_selector: str | None = None,
    ):
        self.editor_url = editor_url
        self.profile_dir = profile_dir
        self.headless = headless
        self.new_article_selector = new_article_selector
        self.title_selector = title_selector
        self.body_selector = body_selector
        self.publish_selector = publish_selector

        self.debug_dir = self.profile_dir.parent / "dzen-debug"
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def _save_debug(self, page: Page, prefix: str) -> tuple[Path, Path]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        screenshot_path = self.debug_dir / f"{prefix}_{stamp}.png"
        html_path = self.debug_dir / f"{prefix}_{stamp}.html"

        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            log.exception("Не удалось сохранить screenshot Дзена")

        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:
            log.exception("Не удалось сохранить HTML Дзена")

        log.error("Dzen debug screenshot: %s", screenshot_path)
        log.error("Dzen debug HTML: %s", html_path)
        return screenshot_path, html_path

    @staticmethod
    def _click_by_text(page: Page, patterns: list[str]) -> bool:
        for pattern in patterns:
            regex = re.compile(pattern, re.I)

            for getter in (
                lambda: page.get_by_role("button", name=regex).first,
                lambda: page.get_by_text(regex, exact=False).first,
            ):
                try:
                    loc = getter()
                    if loc.count() and loc.is_visible(timeout=1200):
                        loc.click()
                        return True
                except Exception:
                    pass

        return False

    @staticmethod
    def _visible_contenteditables(page: Page) -> list[Locator]:
        result: list[tuple[float, float, int, Locator]] = []
        locs = page.locator("[contenteditable='true'], [contenteditable='plaintext-only']")

        for i in range(min(locs.count(), 30)):
            loc = locs.nth(i)
            try:
                if not loc.is_visible(timeout=250):
                    continue

                box = loc.bounding_box()
                if not box:
                    continue

                result.append(
                    (
                        float(box.get("y", 99999)),
                        float(box.get("x", 99999)),
                        i,
                        loc,
                    )
                )
            except Exception:
                continue

        # Сортируем по фактическому положению на странице:
        # верхний редактор должен быть заголовком.
        result.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in result]

    def _wait_for_editor_fields(self, page: Page, timeout_ms: int = 20000) -> list[Locator]:
        deadline = datetime.now().timestamp() + timeout_ms / 1000

        while datetime.now().timestamp() < deadline:
            fields = self._visible_contenteditables(page)
            if len(fields) >= 2:
                log.info("Dzen editor: найдено contenteditable-полей: %s", len(fields))
                return fields
            page.wait_for_timeout(500)

        return self._visible_contenteditables(page)

    def _close_help_popup(self, page: Page) -> None:
        """
        Закрывает обучающее окно Дзена, которое перекрывает редактор
        и не даёт Playwright кликнуть в поле текста.
        """
        selectors = [
            ".ReactModal__Overlay [aria-label='Закрыть']",
            "[class*='help-popup'] [aria-label='Закрыть']",
            "[role='button'][aria-label='Закрыть']",
        ]

        for selector in selectors:
            try:
                loc = page.locator(selector).last
                if loc.count() and loc.is_visible(timeout=700):
                    loc.click(timeout=3000)
                    page.wait_for_timeout(700)
                    log.info("Dzen: обучающее окно закрыто")
                    return
            except Exception:
                continue

    def _open_article_editor(self, page: Page) -> list[Locator]:
        log.info("Dzen stage: открываю студию")
        page.goto(
            self.editor_url,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        page.wait_for_timeout(2500)

        # Если URL уже ведёт прямо в редактор.
        fields = self._visible_contenteditables(page)
        if len(fields) >= 2:
            self._close_help_popup(page)
            log.info("Dzen stage: редактор уже открыт")
            return self._wait_for_editor_fields(page, timeout_ms=5000)

        # На главной странице студии кнопка + имеет стабильный data-testid.
        selector = self.new_article_selector or "[data-testid='add-publication-button']"
        add_button = page.locator(selector).first

        try:
            add_button.wait_for(state="visible", timeout=20000)
        except Exception as exc:
            screenshot, html_file = self._save_debug(page, "add_button_not_visible")
            raise DzenPublishError(
                "Кнопка создания публикации не появилась. "
                f"Скриншот: {screenshot}. HTML: {html_file}"
            ) from exc

        clicked = False

        # Обычный клик.
        try:
            add_button.click(timeout=5000)
            clicked = True
            log.info("Dzen stage: нажата кнопка +")
        except Exception:
            pass

        # Force-click.
        if not clicked:
            try:
                add_button.click(timeout=5000, force=True)
                clicked = True
                log.info("Dzen stage: кнопка + нажата force-click")
            except Exception:
                pass

        # JS click как последний fallback.
        if not clicked:
            try:
                add_button.evaluate("(el) => el.click()")
                clicked = True
                log.info("Dzen stage: кнопка + нажата через JS")
            except Exception as exc:
                screenshot, html_file = self._save_debug(page, "add_button_click_error")
                raise DzenPublishError(
                    "Не удалось нажать кнопку создания публикации. "
                    f"Скриншот: {screenshot}. HTML: {html_file}"
                ) from exc

        page.wait_for_timeout(1200)

        # После плюса обычно появляется выбор формата.
        # Сначала ищем кнопку/пункт «Статья».
        article_clicked = self._click_by_text(
            page,
            [
                r"^Статья$",
                r"Написать статью",
            ],
        )

        if article_clicked:
            log.info("Dzen stage: выбран формат «Статья»")
            page.wait_for_timeout(1800)

        # Иногда плюс сразу открывает редактор — поэтому просто ждём поля.
        fields = self._wait_for_editor_fields(page, timeout_ms=25000)

        if len(fields) < 2:
            screenshot, html_file = self._save_debug(page, "editor_not_opened")
            raise DzenPublishError(
                "После нажатия «+» редактор статьи не открылся. "
                f"Скриншот: {screenshot}. HTML: {html_file}"
            )

        self._close_help_popup(page)
        log.info("Dzen stage: редактор статьи открыт")
        return self._wait_for_editor_fields(page, timeout_ms=5000)

    @staticmethod
    def _replace_content(page: Page, loc: Locator, value: str) -> None:
        """
        Универсальная замена содержимого contenteditable / textarea / input.
        Нужна для заголовка и тела статьи.
        """
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=5000)

        try:
            loc.fill(value)
            page.wait_for_timeout(400)
            return
        except Exception:
            pass

        # Для Draft.js/contenteditable fill() может не работать.
        try:
            page.keyboard.press("Control+A")
            page.wait_for_timeout(120)
            page.keyboard.insert_text(value)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass

        # Последний fallback — через DOM.
        loc.evaluate(
            """(el, value) => {
                el.focus();

                if ('value' in el) {
                    el.value = value;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }

                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                selection.removeAllRanges();
                selection.addRange(range);

                document.execCommand('insertText', false, value);
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                }));
            }""",
            value,
        )
        page.wait_for_timeout(500)

    def _fill_title(self, page: Page, fields: list[Locator], title: str) -> Locator:
        if self.title_selector:
            title_loc = page.locator(self.title_selector).first
        else:
            # Точный селектор текущего Draft.js-редактора Дзена.
            exact_title = page.locator(
                "[contenteditable='true'][role='textbox']"
                ":not([aria-describedby='placeholder-ZenDraftEditor'])"
            ).first
            if exact_title.count() and exact_title.is_visible(timeout=1000):
                title_loc = exact_title
            else:
                title_loc = fields[0]

        try:
            self._replace_content(page, title_loc, title)
        except Exception:
            self._save_debug(page, "title_fill_error")
            raise

        log.info("Dzen: заголовок заполнен")
        return title_loc

    def _fill_body(
        self,
        page: Page,
        fields: list[Locator],
        title_loc: Locator,
        body: str,
    ) -> None:
        if self.body_selector:
            body_loc = page.locator(self.body_selector).first
        else:
            # У тела статьи в текущем редакторе стабильный aria-describedby.
            exact_body = page.locator(
                "[contenteditable='true'][role='textbox']"
                ":has([data-editor='ZenDraftEditor'])"
            ).first

            if not exact_body.count():
                exact_body = page.locator(
                    "[contenteditable='true'][role='textbox']"
                    "[aria-describedby='placeholder-ZenDraftEditor']"
                ).first

            if exact_body.count() and exact_body.is_visible(timeout=1000):
                body_loc = exact_body
            else:
                body_loc = fields[1]

        try:
            self._replace_content(page, body_loc, _plain_text(body))
        except Exception:
            self._save_debug(page, "body_fill_error")
            raise

        log.info("Dzen: текст статьи заполнен")

    @staticmethod
    def _editor_remote_media_urls(page: Page) -> set[str]:
        """
        Возвращает только реальные удалённые URL изображений/медиа внутри
        редактора статьи. Служебные изменения DOM и toolbar здесь не считаются.
        """
        editor = page.locator(
            "[class*='zen-draft-editor__zenEditor']"
        ).first

        if not editor.count():
            return set()

        try:
            urls = editor.evaluate(
                """el => {
                    const result = new Set();

                    const add = value => {
                        if (!value) return;
                        const text = String(value).trim();

                        // srcset может содержать несколько URL.
                        for (const part of text.split(',')) {
                            const candidate = part.trim().split(/\\s+/)[0];
                            if (
                                candidate.startsWith('http://') ||
                                candidate.startsWith('https://')
                            ) {
                                result.add(candidate);
                            }
                        }
                    };

                    for (const img of el.querySelectorAll('img')) {
                        add(img.currentSrc);
                        add(img.src);
                        add(img.getAttribute('src'));
                        add(img.getAttribute('srcset'));
                    }

                    for (const source of el.querySelectorAll('source')) {
                        add(source.getAttribute('src'));
                        add(source.getAttribute('srcset'));
                    }

                    for (const node of el.querySelectorAll('*')) {
                        const attrs = [
                            'data-src',
                            'data-image-src',
                            'data-image-url',
                            'data-original',
                            'poster'
                        ];

                        for (const attr of attrs) {
                            add(node.getAttribute && node.getAttribute(attr));
                        }

                        try {
                            const bg = window.getComputedStyle(node).backgroundImage;
                            if (bg && bg !== 'none') {
                                const matches = bg.match(/url\\(["']?(https?:\\/\\/[^"'\\)]+)["']?\\)/g) || [];
                                for (const item of matches) {
                                    const m = item.match(/https?:\\/\\/[^"'\\)]+/);
                                    if (m) add(m[0]);
                                }
                            }
                        } catch (_) {}
                    }

                    return Array.from(result);
                }"""
            )
            return set(urls or [])
        except Exception:
            return set()

    @staticmethod
    def _editor_blob_media_count(page: Page) -> int:
        """
        Blob/data preview означает, что браузер получил локальный файл,
        но ещё НЕ означает, что Дзен закончил серверную загрузку.
        """
        editor = page.locator(
            "[class*='zen-draft-editor__zenEditor']"
        ).first

        if not editor.count():
            return 0

        try:
            return int(
                editor.evaluate(
                    """el => {
                        let count = 0;

                        for (const img of el.querySelectorAll('img')) {
                            const src = img.currentSrc || img.src || '';
                            if (
                                src.startsWith('blob:') ||
                                src.startsWith('data:')
                            ) {
                                count++;
                            }
                        }

                        return count;
                    }"""
                )
            )
        except Exception:
            return 0

    def _wait_for_image_ready(
        self,
        page: Page,
        before_urls: set[str],
        network_events: list[dict] | None = None,
        timeout_ms: int = 120000,
    ) -> str:
        """
        Ждём реальную серверную загрузку изображения.

        Важное отличие от прошлой версии:
        - blob: preview не считается успехом;
        - если blob исчез без серверного изображения, считаем загрузку
          неуспешной и показываем сетевую причину;
        - ошибки POST/PUT/PATCH теперь не проглатываются.
        """
        network_events = network_events if network_events is not None else []

        deadline = datetime.now().timestamp() + timeout_ms / 1000
        remote_url = None
        saw_blob = False
        last_blob_count = 0

        log.info(
            "Dzen stage: жду серверную загрузку изображения "
            "(до %.0f сек.)",
            timeout_ms / 1000,
        )

        while datetime.now().timestamp() < deadline:
            current_urls = self._editor_remote_media_urls(page)
            new_urls = current_urls - before_urls

            if new_urls:
                remote_url = sorted(new_urls)[0]
                log.info(
                    "Dzen stage: получен удалённый URL изображения: %s",
                    remote_url[:300],
                )
                break

            blob_count = self._editor_blob_media_count(page)

            if blob_count > 0:
                saw_blob = True

            if blob_count != last_blob_count:
                last_blob_count = blob_count
                log.info(
                    "Dzen stage: локальный preview изображения, blob_count=%s",
                    blob_count,
                )

            # Если локальный preview уже был, а затем исчез, при этом
            # серверного изображения нет — ждать ещё минуту бессмысленно.
            if saw_blob and blob_count == 0:
                relevant_errors = [
                    event for event in network_events
                    if event.get("failed")
                    or int(event.get("status") or 0) >= 400
                ]

                details = relevant_errors[-5:] if relevant_errors else network_events[-5:]
                self._save_debug(page, "image_blob_disappeared")

                raise DzenPublishError(
                    "Локальный preview изображения появился, но затем исчез, "
                    "а серверное изображение в редакторе не появилось. "
                    "Это похоже на ошибку сетевой загрузки, а не на нехватку ожидания. "
                    f"Последние сетевые события: {details}"
                )

            # Явная ошибка интерфейса.
            try:
                body_text = page.locator("body").inner_text(timeout=500).lower()
                if (
                    "не удалось загрузить изображение" in body_text
                    or "ошибка загрузки изображения" in body_text
                    or "не удалось загрузить фото" in body_text
                ):
                    self._save_debug(page, "image_upload_ui_error")
                    raise DzenPublishError(
                        "Дзен сообщил об ошибке загрузки изображения. "
                        f"Сетевые события: {network_events[-5:]}"
                    )
            except DzenPublishError:
                raise
            except Exception:
                pass

            page.wait_for_timeout(700)

        if not remote_url:
            self._save_debug(page, "image_remote_url_timeout")
            raise DzenPublishError(
                "За 120 секунд изображение не стало серверным. "
                "Публикация остановлена. "
                f"Последние сетевые события: {network_events[-8:]}"
            )

        # После получения серверного URL ждём окончание внутренней обработки.
        log.info(
            "Dzen stage: серверный URL есть; жду 15 секунд "
            "финальной обработки изображения"
        )
        page.wait_for_timeout(15000)

        current_urls = self._editor_remote_media_urls(page)
        if remote_url not in current_urls:
            self._save_debug(page, "image_disappeared_after_upload")
            raise DzenPublishError(
                "Изображение появилось после загрузки, но затем исчезло."
            )

        self._wait_until_saved(page, timeout_ms=60000)

        log.info(
            "Dzen stage: черновик с изображением сохранён; "
            "контрольная пауза 8 секунд"
        )
        page.wait_for_timeout(8000)

        final_urls = self._editor_remote_media_urls(page)
        if remote_url not in final_urls:
            self._save_debug(page, "image_missing_before_publish")
            raise DzenPublishError(
                "Перед публикацией изображение отсутствует в редакторе."
            )

        log.info(
            "Dzen stage: изображение подтверждено непосредственно перед публикацией"
        )
        return remote_url

    def _try_upload_image(
        self,
        page: Page,
        image_path: Path | None,
    ) -> None:
        if not image_path or not image_path.exists():
            return

        log.info(
            "Dzen image file: path=%s size=%s bytes suffix=%s",
            image_path,
            image_path.stat().st_size,
            image_path.suffix,
        )

        # Закрываем рекламный/onboarding баннер.
        try:
            close_banner = page.locator("[data-testid='close-banner']").first
            if close_banner.count() and close_banner.is_visible(timeout=500):
                close_banner.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            pass

        # После заполнения Draft.js aria-describedby может исчезать.
        body = page.locator(
            "[contenteditable='true'][role='textbox']"
            ":has([data-editor='ZenDraftEditor'])"
        ).first

        if not body.count():
            body = page.locator(
                "[class*='zen-draft-editor__zenEditor'] "
                "[contenteditable='true'][role='textbox']"
            ).first

        if not body.count():
            editables = page.locator(
                "[contenteditable='true'][role='textbox']"
            )
            if editables.count() >= 2:
                body = editables.nth(1)

        if not body.count():
            self._save_debug(page, "image_no_body")
            raise DzenPublishError(
                "Не найдено поле статьи для вставки изображения."
            )

        try:
            log.info(
                "Dzen: найдено поле статьи для изображения, html=%s",
                body.evaluate("el => el.outerHTML.slice(0, 220)"),
            )
        except Exception:
            pass

        # Ставим caret в самый конец и создаём пустой абзац.
        try:
            body.scroll_into_view_if_needed(timeout=5000)
            body.click(timeout=5000)
            body.evaluate(
                """el => {
                    el.focus();
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    range.collapse(false);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                }"""
            )
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
        except Exception as exc:
            self._save_debug(page, "image_create_empty_line_error")
            raise DzenPublishError(
                "Не удалось создать пустую строку для изображения."
            ) from exc

        image_button = page.locator(
            "button[data-tip='Вставить изображение']"
        ).first

        visible = False
        for _ in range(12):
            try:
                if image_button.count() and image_button.is_visible(timeout=300):
                    visible = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(250)

        if not visible:
            try:
                body.evaluate(
                    """el => {
                        el.focus();
                        const range = document.createRange();
                        range.selectNodeContents(el);
                        range.collapse(false);
                        const selection = window.getSelection();
                        selection.removeAllRanges();
                        selection.addRange(range);
                    }"""
                )
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
                visible = (
                    image_button.count()
                    and image_button.is_visible(timeout=1500)
                )
            except Exception:
                pass

        if not visible:
            self._save_debug(page, "image_button_hidden")
            raise DzenPublishError(
                "Кнопка «Вставить изображение» не появилась на пустой строке."
            )

        before_urls = self._editor_remote_media_urls(page)
        log.info(
            "Dzen stage: медиа-URL до изображения: %s",
            list(before_urls),
        )

        # В текущем интерфейсе кнопка НЕ обязана открывать нативный filechooser.
        # Поэтому больше не ждём page.expect_file_chooser().
        #
        # Вместо этого нажимаем кнопку и работаем напрямую с появившимся
        # input[type=file].
        network_events: list[dict] = []

        def on_request_failed(request):
            try:
                event = {
                    "kind": "requestfailed",
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "failure": request.failure,
                    "failed": True,
                }
                network_events.append(event)
                log.error(
                    "DZEN IMAGE NET FAILED | %s | %s | %s",
                    request.method,
                    request.url,
                    request.failure,
                )
            except Exception:
                pass

        def on_response(response):
            try:
                request = response.request
                method = request.method.upper()

                if method in {"POST", "PUT", "PATCH"} or response.status >= 400:
                    event = {
                        "kind": "response",
                        "method": method,
                        "url": response.url,
                        "status": response.status,
                        "resource_type": request.resource_type,
                        "failed": response.status >= 400,
                    }
                    network_events.append(event)

                    level = log.error if response.status >= 400 else log.info
                    level(
                        "DZEN IMAGE NET | %s %s | %s",
                        method,
                        response.status,
                        response.url,
                    )
            except Exception:
                pass

        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

        try:
            # Нажатие создаёт/активирует скрытый input.
            try:
                image_button.click(timeout=5000)
            except Exception:
                image_button.click(timeout=5000, force=True)

            page.wait_for_timeout(600)

            inputs = page.locator("input[type='file']")
            input_count = inputs.count()

            if input_count == 0:
                self._save_debug(page, "image_no_file_input")
                raise DzenPublishError(
                    "После нажатия «Вставить изображение» "
                    "не появился input[type=file]."
                )

            log.info("Dzen image: найдено input[type=file]: %s", input_count)

            chosen = None

            # Сначала выбираем input, который явно принимает изображения.
            for i in range(input_count):
                inp = inputs.nth(i)
                try:
                    accept = (inp.get_attribute("accept") or "").lower()
                    multiple = inp.get_attribute("multiple")
                    log.info(
                        "Dzen image input #%s: accept=%r multiple=%r",
                        i,
                        accept,
                        multiple,
                    )

                    if (
                        "image" in accept
                        or ".jpg" in accept
                        or ".jpeg" in accept
                        or ".png" in accept
                        or accept == ""
                    ):
                        chosen = inp
                        if "image" in accept:
                            break
                except Exception:
                    continue

            if chosen is None:
                chosen = inputs.last

            # ВАЖНО: это реальная передача файла в image input.
            chosen.set_input_files(
                str(image_path),
                timeout=10000,
            )
            log.info(
                "Dzen stage: файл передан непосредственно в image input"
            )

            self._wait_for_image_ready(
                page,
                before_urls,
                network_events=network_events,
                timeout_ms=120000,
            )

            log.info(
                "Dzen: изображение подтверждено, загружено и сохранено в статье"
            )

        except Exception as exc:
            self._save_debug(page, "image_upload_error")

            # Здесь больше не возвращаем старую ошибку filechooser.
            # Пользователь увидит реальную причину загрузки.
            raise DzenPublishError(
                f"Не удалось загрузить изображение в Дзен: {exc}. "
                f"Сетевые события: {network_events[-10:]}"
            ) from exc

        finally:
            # Убираем временные сетевые обработчики.
            try:
                page.remove_listener("requestfailed", on_request_failed)
            except Exception:
                pass
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    def _wait_until_saved(self, page: Page, timeout_ms: int = 45000) -> None:
        """
        Ждём, пока Дзен закончит автосохранение черновика.
        """
        deadline = datetime.now().timestamp() + timeout_ms / 1000
        last_text = ""

        while datetime.now().timestamp() < deadline:
            try:
                status = page.locator(
                    "span[class*='editor-header__status']"
                ).first

                if status.count():
                    last_text = status.inner_text(timeout=700).strip()
                    normalized = last_text.lower().replace("ё", "е")

                    if "идет сохранение" in normalized:
                        page.wait_for_timeout(500)
                        continue

                    if last_text:
                        log.info("Dzen stage: статус сохранения — %s", last_text)
                    return

                # Если элемента статуса временно нет, ещё немного ждём.
                page.wait_for_timeout(500)

            except Exception:
                page.wait_for_timeout(500)

        raise DzenPublishError(
            "Дзен не завершил сохранение черновика за 45 секунд. "
            f"Последний статус: {last_text or 'не определён'}"
        )

    def _publish(self, page: Page) -> None:
        self._wait_until_saved(page)

        selector = self.publish_selector or "[data-testid='article-publish-btn']"
        publish_btn = page.locator(selector).first

        try:
            publish_btn.wait_for(state="visible", timeout=10000)
        except Exception as exc:
            screenshot, html_file = self._save_debug(
                page,
                "publish_button_not_visible",
            )
            raise DzenPublishError(
                "Кнопка «Опубликовать» не видна. "
                f"Скриншот: {screenshot}. HTML: {html_file}"
            ) from exc

        try:
            disabled = publish_btn.is_disabled(timeout=1500)
        except Exception:
            disabled = False

        if disabled:
            raise DzenPublishError(
                "Кнопка «Опубликовать» осталась disabled "
                "после сохранения черновика."
            )

        publish_net: list[dict] = []
        publish_confirmed = {"value": False, "reason": ""}
        publish_error = {"value": False, "details": None}

        def _body_proves_published(body_text: str) -> bool:
            value = (body_text or "").lower().replace(" ", "")
            return any(
                marker in value
                for marker in (
                    '"status":"published"',
                    '"publicationstatus":"published"',
                    '"ispublished":true',
                    '"published":true',
                    '"publishedat":',
                    '"publicationurl":',
                )
            )

        def _url_is_publish_action(url: str) -> bool:
            value = (url or "").lower()

            # ВАЖНО:
            # update-publication-content-and-publish — это как раз
            # финальный endpoint публикации, его нельзя отфильтровывать
            # как обычное автосохранение.
            if "update-publication-content-and-publish" in value:
                return True

            if any(
                x in value
                for x in (
                    "/update-publication-content?",
                    "/add-image",
                    "/log-studio-event",
                    "/log-editor-event",
                    "heartbeat",
                )
            ):
                return False

            return bool(
                re.search(
                    r"(?:/|-)publish(?:/|\?|$|-)"
                    r"|publication/(?:publish|submit|start)"
                    r"|publish-publication"
                    r"|publish-article",
                    value,
                )
            )

        def on_publish_response(response):
            try:
                request = response.request
                method = request.method.upper()
                url = response.url

                if method not in {"POST", "PUT", "PATCH"}:
                    return

                if "/editor-api/" not in url:
                    return

                body_snippet = ""
                try:
                    body_snippet = response.text()[:4000]
                except Exception:
                    pass

                post_data = ""
                try:
                    post_data = request.post_data or ""
                    post_data = post_data[:4000]
                except Exception:
                    pass

                event = {
                    "method": method,
                    "status": response.status,
                    "url": url,
                    "body": body_snippet[:2000],
                    "request_body": post_data[:2000],
                }
                publish_net.append(event)

                if response.status >= 400:
                    log.error(
                        "DZEN PUBLISH NET | %s %s | %s | RESPONSE=%r | REQUEST=%r",
                        method,
                        response.status,
                        url,
                        body_snippet[:1800],
                        post_data[:1800],
                    )

                    publish_error["value"] = True
                    publish_error["details"] = event
                    return

                log.info(
                    "DZEN PUBLISH NET | %s %s | %s",
                    method,
                    response.status,
                    url,
                )

                if 200 <= response.status < 300:
                    if _url_is_publish_action(url):
                        publish_confirmed["value"] = True
                        publish_confirmed["reason"] = (
                            f"publish endpoint {response.status}: {url}"
                        )
                    elif _body_proves_published(body_snippet):
                        publish_confirmed["value"] = True
                        publish_confirmed["reason"] = (
                            f"API response confirms published: {url}"
                        )

            except Exception as exc:
                log.warning(
                    "Dzen publish response parser error: %s",
                    exc,
                )

        def on_publish_failed(request):
            try:
                if request.method.upper() not in {"POST", "PUT", "PATCH"}:
                    return
                if "/editor-api/" not in request.url:
                    return

                event = {
                    "method": request.method.upper(),
                    "status": 0,
                    "url": request.url,
                    "failure": request.failure,
                }
                publish_net.append(event)

                publish_error["value"] = True
                publish_error["details"] = event

                log.error(
                    "DZEN PUBLISH NET FAILED | %s | %s | %s",
                    request.method,
                    request.url,
                    request.failure,
                )
            except Exception:
                pass

        def ui_error_text() -> str:
            """
            Собираем видимые сообщения ошибки после ответа 400.
            """
            parts = []

            selectors = (
                "[role='alert']",
                ".notifications-wrapper",
                "[class*='snackbar']",
                "[class*='error']",
            )

            for selector in selectors:
                try:
                    loc = page.locator(selector)
                    for i in range(min(loc.count(), 20)):
                        item = loc.nth(i)
                        try:
                            if not item.is_visible(timeout=80):
                                continue

                            txt = " ".join(
                                (item.inner_text(timeout=200) or "").split()
                            )
                            if txt and txt not in parts:
                                parts.append(txt)
                        except Exception:
                            continue
                except Exception:
                    continue

            return " | ".join(parts)[:2000]

        def ui_confirms_published() -> bool:
            try:
                body_text = page.locator("body").inner_text(
                    timeout=700
                ).lower()

                markers = (
                    "статья опубликована",
                    "публикация опубликована",
                    "успешно опубликовано",
                    "опубликовано успешно",
                )
                return any(marker in body_text for marker in markers)
            except Exception:
                return False

        page.on("response", on_publish_response)
        page.on("requestfailed", on_publish_failed)

        before_url = page.url

        try:
            log.info("Dzen stage: нажимаю «Опубликовать»")

            try:
                publish_btn.click(timeout=7000)
                log.info(
                    "Dzen stage: клик по «Опубликовать» выполнен"
                )
            except Exception as exc:
                try:
                    publish_btn.evaluate("(el) => el.click()")
                    log.info(
                        "Dzen stage: «Опубликовать» нажата через JS"
                    )
                except Exception:
                    screenshot, html_file = self._save_debug(
                        page,
                        "publish_click_error",
                    )
                    raise DzenPublishError(
                        "Не удалось нажать «Опубликовать». "
                        f"Скриншот: {screenshot}. HTML: {html_file}"
                    ) from exc

            clicked_signatures: set[str] = set()
            deadline = datetime.now().timestamp() + 30000
            last_ui_log = 0.0

            while datetime.now().timestamp() < deadline:
                page.wait_for_timeout(500)

                # В v16 этого не было — поэтому после HTTP 400 цикл
                # продолжал печатать кнопки и казался зависшим.
                if publish_error["value"]:
                    page.wait_for_timeout(700)
                    ui_error = ui_error_text()

                    screenshot, html_file = self._save_debug(
                        page,
                        "publish_api_400",
                    )

                    details = publish_error["details"]

                    raise DzenPublishError(
                        "Дзен отклонил публикацию на сервере. "
                        f"Ответ API: {details}. "
                        f"Сообщение интерфейса: {ui_error or 'не найдено'}. "
                        f"Скриншот: {screenshot}. HTML: {html_file}"
                    )

                if publish_confirmed["value"]:
                    log.info(
                        "Dzen stage: сервер подтвердил публикацию — %s",
                        publish_confirmed["reason"],
                    )
                    page.wait_for_timeout(1800)
                    return

                if ui_confirms_published():
                    log.info(
                        "Dzen stage: интерфейс подтвердил публикацию"
                    )
                    return

                try:
                    current_url = page.url
                    if (
                        current_url != before_url
                        and not current_url.rstrip("/").endswith("/edit")
                    ):
                        log.info(
                            "Dzen stage: редактор закрыт после публикации: %s",
                            current_url,
                        )
                        return
                except Exception:
                    pass

                candidates = []

                for pattern in (
                    r"^Опубликовать$",
                    r"^Продолжить$",
                    r"^Далее$",
                    r"^Готово$",
                ):
                    try:
                        loc = page.get_by_role(
                            "button",
                            name=re.compile(pattern, re.I),
                        )

                        for i in range(loc.count()):
                            btn = loc.nth(i)
                            try:
                                if not btn.is_visible(timeout=200):
                                    continue

                                testid = (
                                    btn.get_attribute("data-testid")
                                    or ""
                                )

                                if testid == "article-publish-btn":
                                    continue

                                text_value = " ".join(
                                    (
                                        btn.inner_text(timeout=200)
                                        or ""
                                    ).split()
                                )

                                box = btn.bounding_box()
                                signature = (
                                    f"{text_value}|{testid}|"
                                    f"{round(box['x']) if box else -1}|"
                                    f"{round(box['y']) if box else -1}"
                                )

                                if signature in clicked_signatures:
                                    continue

                                candidates.append(
                                    (btn, signature, text_value, testid)
                                )
                            except Exception:
                                continue
                    except Exception:
                        continue

                if candidates:
                    btn, signature, text_value, testid = candidates[-1]

                    try:
                        log.info(
                            "Dzen stage: нажимаю финальную кнопку %r "
                            "(data-testid=%r)",
                            text_value,
                            testid,
                        )
                        btn.click(timeout=5000)
                        clicked_signatures.add(signature)
                        continue
                    except Exception:
                        pass

                # Не спамим лог каждую секунду.
                now = datetime.now().timestamp()
                if now - last_ui_log >= 5:
                    last_ui_log = now
                    try:
                        publish_candidates = page.locator(
                            "[data-testid*='publish']"
                        )
                        debug_buttons = []
                        for i in range(min(publish_candidates.count(), 20)):
                            b = publish_candidates.nth(i)
                            try:
                                if b.is_visible(timeout=100):
                                    debug_buttons.append(
                                        {
                                            "text": " ".join(
                                                (
                                                    b.inner_text(timeout=150)
                                                    or ""
                                                ).split()
                                            )[:100],
                                            "testid": (
                                                b.get_attribute("data-testid")
                                                or ""
                                            ),
                                            "disabled": b.is_disabled(
                                                timeout=150
                                            ),
                                        }
                                    )
                            except Exception:
                                continue

                        log.info(
                            "Dzen publish waiting; buttons=%s",
                            debug_buttons,
                        )
                    except Exception:
                        pass

            screenshot, html_file = self._save_debug(
                page,
                "publish_not_really_published",
            )

            raise DzenPublishError(
                "Дзен не подтвердил фактическую публикацию за 30 секунд. "
                f"URL: {page.url}. "
                f"Сетевые события: {publish_net[-15:]}. "
                f"Скриншот: {screenshot}. HTML: {html_file}"
            )

        finally:
            try:
                page.remove_listener(
                    "response",
                    on_publish_response,
                )
            except Exception:
                pass

            try:
                page.remove_listener(
                    "requestfailed",
                    on_publish_failed,
                )
            except Exception:
                pass

    def publish(
        self,
        *,
        title: str,
        body: str,
        image_path: str | None = None,
    ) -> str:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        image = Path(image_path) if image_path else None
        stage = "launch_browser"

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
                args=["--disable-blink-features=AutomationControlled"],
            )

            page = context.pages[0] if context.pages else context.new_page()

            try:
                stage = "open_article_editor"
                fields = self._open_article_editor(page)

                stage = "fill_body"
                title_loc = fields[0]
                self._fill_body(page, fields, title_loc, body)

                stage = "refresh_fields_after_body"
                fields_after_body = self._wait_for_editor_fields(
                    page,
                    timeout_ms=5000,
                )
                if len(fields_after_body) >= 2:
                    fields = fields_after_body

                stage = "fill_title"
                self._fill_title(page, fields, title)

                stage = "upload_image"
                self._try_upload_image(page, image)

                stage = "publish"
                self._publish(page)

                current_url = page.url
                log.info(
                    "Dzen publish SUCCESS. final_url=%s",
                    current_url,
                )
                return current_url

            except Exception as exc:
                try:
                    current_url = page.url
                except Exception:
                    current_url = "<unavailable>"

                log.exception(
                    "Dzen publish FAILED. stage=%s url=%s error=%s",
                    stage,
                    current_url,
                    exc,
                )
                self._save_debug(page, f"publish_error_{stage}")
                raise
            finally:
                context.close()
