from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, async_playwright

from config import Settings
from dzen_selectors import (
    AUTHOR_SELECTORS,
    COMMENT_CONTAINER_SELECTORS,
    COMMENT_TEXT_SELECTORS,
    PUBLICATION_LINK_SELECTORS,
    REPLY_BUTTON_SELECTORS,
    REPLY_INPUT_SELECTORS,
    SEND_BUTTON_SELECTORS,
    STUDIO_COMMENTS_LINK_SELECTORS,
)
from models import DzenComment
from reply_policy import stable_comment_id

log = logging.getLogger(__name__)


class DzenAuthError(RuntimeError):
    pass


class DzenBrowser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.browser:
            return
        self.settings.debug_dir.mkdir(parents=True, exist_ok=True)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=self.settings.dzen_headless,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        storage_state = str(self.settings.auth_state_path) if self.settings.auth_state_path.exists() else None
        self.context = await self.browser.new_context(
            storage_state=storage_state,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self.context.new_page()

    async def close(self) -> None:
        if self.context:
            try:
                await self.context.storage_state(path=str(self.settings.auth_state_path))
            except Exception:
                pass
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        self.page = None
        self.context = None
        self.browser = None
        self.pw = None

    async def ensure_started(self) -> None:
        if not self.browser:
            await self.start()

    async def check_auth(self) -> tuple[bool, str]:
        async with self._lock:
            await self.ensure_started()
            assert self.page
            try:
                await self.page.goto(self.settings.dzen_studio_url, wait_until="domcontentloaded", timeout=60000)
                await self.page.wait_for_timeout(2500)
                url = self.page.url.lower()
                body = (await self.page.locator("body").inner_text(timeout=10000)).lower()
                if "passport.yandex" in url or "войти" in body[:2500] and "студ" not in body[:2500]:
                    return False, self.page.url
                return True, self.page.url
            except Exception as exc:
                return False, f"Ошибка проверки: {exc}"

    async def collect_new_comments(self, limit: int) -> list[DzenComment]:
        async with self._lock:
            await self.ensure_started()
            assert self.page
            comments: list[DzenComment] = []
            try:
                studio = await self._collect_from_studio(limit)
                comments.extend(studio)
            except DzenAuthError:
                raise
            except Exception:
                log.exception("Studio comments collection failed")
                await self._debug_snapshot("studio_collect_error")

            if len(comments) < limit:
                try:
                    pub_comments = await self._collect_from_publications(limit - len(comments))
                    known = {c.comment_id for c in comments}
                    comments.extend(c for c in pub_comments if c.comment_id not in known)
                except DzenAuthError:
                    raise
                except Exception:
                    log.exception("Publication fallback collection failed")
                    await self._debug_snapshot("publication_collect_error")
            return comments[:limit]

    async def _collect_from_studio(self, limit: int) -> list[DzenComment]:
        assert self.page
        target = self.settings.dzen_comments_url or self.settings.dzen_studio_url
        await self.page.goto(target, wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_timeout(2500)
        self._raise_if_login(self.page)

        if not self.settings.dzen_comments_url:
            for selector in STUDIO_COMMENTS_LINK_SELECTORS:
                loc = self.page.locator(selector).first
                if await loc.count():
                    try:
                        await loc.click(timeout=5000)
                        await self.page.wait_for_load_state("domcontentloaded", timeout=20000)
                        await self.page.wait_for_timeout(1800)
                        break
                    except Exception:
                        continue

        return await self._extract_comments_from_page(self.page, source="studio", limit=limit)

    async def _collect_from_publications(self, limit: int) -> list[DzenComment]:
        assert self.page and self.context
        await self.page.goto(self.settings.dzen_studio_url, wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_timeout(2200)
        self._raise_if_login(self.page)

        links: list[str] = []
        for selector in PUBLICATION_LINK_SELECTORS:
            loc = self.page.locator(selector)
            count = min(await loc.count(), self.settings.max_publications_to_scan * 3)
            for i in range(count):
                href = await loc.nth(i).get_attribute("href")
                if not href:
                    continue
                href = urljoin("https://dzen.ru", href)
                if self._is_publication_url(href) and href not in links:
                    links.append(href)
                if len(links) >= self.settings.max_publications_to_scan:
                    break
            if len(links) >= self.settings.max_publications_to_scan:
                break

        result: list[DzenComment] = []
        for url in links:
            if len(result) >= limit:
                break
            p = await self.context.new_page()
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=50000)
                await p.wait_for_timeout(1300)
                # Comments are often lazy-loaded near the bottom.
                await p.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await p.wait_for_timeout(1200)
                result.extend(await self._extract_comments_from_page(p, source="publication", limit=limit-len(result)))
            except Exception:
                log.exception("Failed publication scan: %s", url)
            finally:
                await p.close()
        return result

    async def _extract_comments_from_page(self, page: Page, source: str, limit: int) -> list[DzenComment]:
        containers = None
        selector_used = ""
        for selector in COMMENT_CONTAINER_SELECTORS:
            loc = page.locator(selector)
            count = await loc.count()
            if count:
                containers = loc
                selector_used = selector
                break
        if containers is None:
            return []

        title, article_context = await self._extract_article_context(page)
        publication_url = self._canonical_publication_url(page.url)
        out: list[DzenComment] = []
        count = min(await containers.count(), 80)
        for idx in range(count):
            if len(out) >= limit:
                break
            card = containers.nth(idx)
            try:
                reply_button = await self._first_existing(card, REPLY_BUTTON_SELECTORS)
                if reply_button is None:
                    continue
                text = await self._extract_text(card)
                if not text or len(text) > 5000:
                    continue
                author = await self._extract_author(card)
                if self.settings.dzen_author_name and author.strip().lower() == self.settings.dzen_author_name.strip().lower():
                    continue
                pub_url = await self._extract_publication_link(card) or publication_url
                dom_id = (
                    await card.get_attribute("data-comment-id")
                    or await card.get_attribute("data-id")
                    or await card.get_attribute("id")
                    or ""
                )
                comment_id = stable_comment_id(pub_url, author, text, dom_id)
                hint = f"{selector_used}::{idx}"
                out.append(
                    DzenComment(
                        comment_id=comment_id,
                        author=author,
                        text=text,
                        publication_title=title,
                        publication_url=pub_url,
                        article_context=article_context,
                        source="studio" if source == "studio" else "publication",
                        reply_locator_hint=hint,
                    )
                )
            except Exception:
                continue
        return out

    async def enrich_comment_context(self, comment: DzenComment) -> DzenComment:
        if comment.article_context and len(comment.article_context) > 500:
            return comment
        if not comment.publication_url or not self.context:
            return comment
        async with self._lock:
            p = await self.context.new_page()
            try:
                await p.goto(comment.publication_url, wait_until="domcontentloaded", timeout=50000)
                await p.wait_for_timeout(1200)
                title, context = await self._extract_article_context(p)
                if title:
                    comment.publication_title = title
                if context:
                    comment.article_context = context
                return comment
            finally:
                await p.close()

    async def publish_reply(self, comment: DzenComment, reply_text: str) -> bool:
        async with self._lock:
            await self.ensure_started()
            assert self.page
            # Re-open the source and re-find by exact normalized comment text. This is safer
            # than keeping stale locators between AI calls.
            if comment.source == "studio":
                target = self.settings.dzen_comments_url or self.settings.dzen_studio_url
                await self.page.goto(target, wait_until="domcontentloaded", timeout=60000)
                await self.page.wait_for_timeout(1800)
                if not self.settings.dzen_comments_url:
                    for selector in STUDIO_COMMENTS_LINK_SELECTORS:
                        loc = self.page.locator(selector).first
                        if await loc.count():
                            try:
                                await loc.click(timeout=4000)
                                await self.page.wait_for_timeout(1500)
                                break
                            except Exception:
                                pass
            else:
                await self.page.goto(comment.publication_url, wait_until="domcontentloaded", timeout=60000)
                await self.page.wait_for_timeout(1200)
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.page.wait_for_timeout(1000)

            self._raise_if_login(self.page)
            card = await self._find_comment_card(self.page, comment)
            if card is None:
                await self._debug_snapshot(f"comment_not_found_{comment.comment_id[:8]}")
                return False

            reply_button = await self._first_existing(card, REPLY_BUTTON_SELECTORS)
            if reply_button is None:
                return False
            await reply_button.click(timeout=8000)
            await self.page.wait_for_timeout(500)

            input_loc = await self._first_existing(card, REPLY_INPUT_SELECTORS)
            if input_loc is None:
                input_loc = await self._first_existing(self.page.locator("body"), REPLY_INPUT_SELECTORS)
            if input_loc is None:
                await self._debug_snapshot(f"reply_input_missing_{comment.comment_id[:8]}")
                return False

            tag = await input_loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "textarea" or tag == "input":
                await input_loc.fill(reply_text)
            else:
                await input_loc.click()
                await input_loc.press("Control+A")
                await input_loc.fill(reply_text)

            send_button = await self._first_existing(card, SEND_BUTTON_SELECTORS)
            if send_button is None:
                send_button = await self._first_existing(self.page.locator("body"), SEND_BUTTON_SELECTORS)
            if send_button is None:
                await self._debug_snapshot(f"send_missing_{comment.comment_id[:8]}")
                return False
            await send_button.click(timeout=8000)
            await self.page.wait_for_timeout(1200)
            await self.context.storage_state(path=str(self.settings.auth_state_path))
            return True

    async def _find_comment_card(self, page: Page, comment: DzenComment) -> Locator | None:
        normalized_target = " ".join(comment.text.split())
        for selector in COMMENT_CONTAINER_SELECTORS:
            loc = page.locator(selector)
            count = min(await loc.count(), 100)
            for i in range(count):
                card = loc.nth(i)
                try:
                    visible = " ".join((await card.inner_text(timeout=1500)).split())
                    if normalized_target and normalized_target in visible:
                        if comment.author and comment.author not in visible:
                            # Text is the primary key; author is an additional guard when available.
                            continue
                        return card
                except Exception:
                    continue
        return None

    async def _extract_article_context(self, page: Page) -> tuple[str, str]:
        title = ""
        for sel in ["h1", 'meta[property="og:title"]', "title"]:
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                if sel.startswith("meta"):
                    title = (await loc.get_attribute("content") or "").strip()
                else:
                    title = (await loc.inner_text(timeout=1500)).strip()
                if title:
                    break
            except Exception:
                pass

        chunks: list[str] = []
        selectors = [
            'article p',
            '[data-testid*="article" i] p',
            '[class*="article"] p',
            'main p',
        ]
        for sel in selectors:
            loc = page.locator(sel)
            count = min(await loc.count(), 150)
            if not count:
                continue
            for i in range(count):
                try:
                    text = " ".join((await loc.nth(i).inner_text(timeout=700)).split())
                    if len(text) >= 20 and text not in chunks:
                        chunks.append(text)
                except Exception:
                    pass
            if chunks:
                break
        context = "\n".join(chunks)
        return title[:500], context[: self.settings.max_article_chars]

    async def _extract_text(self, card: Locator) -> str:
        for sel in COMMENT_TEXT_SELECTORS:
            loc = card.locator(sel)
            count = min(await loc.count(), 12)
            for i in range(count):
                try:
                    text = " ".join((await loc.nth(i).inner_text(timeout=800)).split())
                    if text and text.lower() not in {"ответить", "пожаловаться", "поделиться"}:
                        # Prefer substantial paragraph-like text.
                        if len(text) >= 2:
                            return text[:5000]
                except Exception:
                    pass
        # Fallback strips common UI labels from card text.
        raw = " ".join((await card.inner_text(timeout=1200)).split())
        raw = re.sub(r"\b(Ответить|Пожаловаться|Поделиться)\b", "", raw, flags=re.I)
        return " ".join(raw.split())[:5000]

    async def _extract_author(self, card: Locator) -> str:
        for sel in AUTHOR_SELECTORS:
            loc = card.locator(sel).first
            if await loc.count():
                try:
                    text = " ".join((await loc.inner_text(timeout=700)).split())
                    if text:
                        return text[:200]
                except Exception:
                    pass
        return ""

    async def _extract_publication_link(self, card: Locator) -> str:
        loc = card.locator('a[href*="dzen.ru"], a[href*="/a/"], a[href*="/video/"], a[href*="/shorts/"]')
        count = min(await loc.count(), 10)
        for i in range(count):
            href = await loc.nth(i).get_attribute("href")
            if href:
                full = urljoin("https://dzen.ru", href)
                if self._is_publication_url(full):
                    return self._canonical_publication_url(full)
        return ""

    @staticmethod
    async def _first_existing(root: Locator, selectors: list[str]) -> Locator | None:
        for sel in selectors:
            loc = root.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=500):
                    return loc
            except Exception:
                continue
        return None

    def _raise_if_login(self, page: Page) -> None:
        url = page.url.lower()
        if "passport.yandex" in url or "auth" in url and "dzen.ru" not in url:
            raise DzenAuthError("Сессия Дзена истекла. Повторно сохраните data/dzen_state.json")

    @staticmethod
    def _is_publication_url(url: str) -> bool:
        path = urlparse(url).path
        return any(token in path for token in ("/a/", "/video/", "/shorts/"))

    @staticmethod
    def _canonical_publication_url(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme or 'https'}://{parsed.netloc or 'dzen.ru'}{parsed.path}" if parsed.path else url

    async def _debug_snapshot(self, name: str) -> None:
        if not self.page:
            return
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)[:80]
        try:
            await self.page.screenshot(path=str(self.settings.debug_dir / f"{safe}.png"), full_page=True)
            html = await self.page.content()
            (self.settings.debug_dir / f"{safe}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass
