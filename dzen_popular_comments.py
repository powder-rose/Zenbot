from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from dzen_browser_lock import DZEN_BROWSER_LOCK

log = logging.getLogger(__name__)

_TOPIC_SYSTEM_PROMPT = """
Ты выбираешь тему для экспертной статьи по комментарию читателя Дзена.
Верни только одну короткую тему статьи на русском языке, без кавычек, без пояснений,
без слов «комментарий», «читатель», «Дзен». Тема должна передавать вопрос, проблему
или тезис из комментария и быть пригодной для дальнейшего поиска фактов.
Если комментарий не содержит содержательной темы для статьи (только благодарность,
эмодзи, оскорбление, реклама, бессвязный текст), верни ровно: SKIP
""".strip()

_UI_WORDS = {
    "ответить", "поделиться", "пожаловаться", "скрыть", "ещё", "еще",
    "нравится", "лайк", "лайки", "комментарии", "комментарий",
}

_TEXT_KEYS = (
    "text", "commentText", "comment_text", "message", "body", "content",
    "plainText", "plain_text", "description",
)
_ID_KEYS = ("commentId", "comment_id", "id", "uuid")
_LIKE_KEYS = (
    "likesCount", "likes_count", "likeCount", "like_count", "likes",
    "positiveCount", "positive_count", "upvotes", "rating", "reactionsCount",
    "reactions_count",
)


@dataclass(slots=True)
class DzenComment:
    key: str
    text: str
    likes: int
    comment_id: str = ""
    author: str = ""
    source_url: str = ""


class DzenPopularCommentSource:
    """Reads Dzen comments and ranks them by likes.

    Preferred mode is public channel scanning, so no Dzen password/session is needed.
    If DZEN_COMMENTS_URL is set, that page is scanned directly (it may be an
    authenticated Author Studio comments page). Otherwise recent article pages are
    collected from DZEN_CHANNEL_URL and scanned one by one.
    """

    def __init__(self) -> None:
        self.channel_url = os.getenv(
            "DZEN_CHANNEL_URL", "https://dzen.ru/profile/specons"
        ).strip()
        self.comments_url = os.getenv("DZEN_COMMENTS_URL", "").strip()
        self.profile_dir = os.getenv("DZEN_COMMENT_PROFILE_DIR", "").strip()
        self.headless = os.getenv("DZEN_COMMENT_HEADLESS", "true").strip().lower() not in {
            "0", "false", "no", "off"
        }
        self.article_limit = max(1, int(os.getenv("DZEN_COMMENT_ARTICLE_LIMIT", "12")))
        self.scroll_rounds = max(2, int(os.getenv("DZEN_COMMENT_SCROLLS", "10")))
        self.debug_dir = Path(os.getenv("DZEN_COMMENT_DEBUG_DIR", "data/dzen_comment_debug"))
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    async def ranked_comments(self) -> list[DzenComment]:
        async with DZEN_BROWSER_LOCK:
            return await self._ranked_comments_unlocked()

    async def _ranked_comments_unlocked(self) -> list[DzenComment]:
        candidates: dict[str, DzenComment] = {}
        async with async_playwright() as pw:
            if self.profile_dir:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(Path(self.profile_dir)),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 1000},
                    locale="ru-RU",
                )
                close_context = True
            else:
                browser = await pw.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000}, locale="ru-RU"
                )
                close_context = True

            try:
                page = context.pages[0] if context.pages else await context.new_page()
                if self.comments_url:
                    await self._scan_page(page, self.comments_url, candidates, open_comments=False)
                else:
                    article_urls = await self._collect_article_urls(page)
                    if not article_urls:
                        raise RuntimeError(
                            "Не удалось найти ссылки на статьи в Dzen-профиле. "
                            "Проверь DZEN_CHANNEL_URL."
                        )
                    for url in article_urls[: self.article_limit]:
                        try:
                            await self._scan_page(page, url, candidates, open_comments=True)
                        except Exception:
                            log.exception("Dzen comments: не удалось просканировать %s", url)
            except Exception:
                await self._save_debug(page, "scan_error")
                raise
            finally:
                if close_context:
                    await context.close()

        values = list(candidates.values())
        values.sort(key=lambda x: (x.likes, len(x.text)), reverse=True)
        return values

    async def _collect_article_urls(self, page: Page) -> list[str]:
        await page.goto(self.channel_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        self._assert_not_login(page)

        stable = 0
        last_height = 0
        for _ in range(max(4, self.scroll_rounds // 2)):
            height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == height == last_height:
                stable += 1
            else:
                stable = 0
            last_height = new_height
            if stable >= 2:
                break

        hrefs: list[str] = await page.eval_on_selector_all(
            "a[href]", "els => els.map(a => a.href).filter(Boolean)"
        )
        result: list[str] = []
        seen: set[str] = set()
        for href in hrefs:
            try:
                parsed = urlparse(href)
            except Exception:
                continue
            if "dzen.ru" not in parsed.netloc:
                continue
            path = parsed.path or ""
            # Dzen article links currently use /a/. Keep a couple of fallbacks.
            if not ("/a/" in path or "/article/" in path):
                continue
            clean = href.split("#", 1)[0]
            if clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    async def _scan_page(
        self,
        page: Page,
        url: str,
        candidates: dict[str, DzenComment],
        *,
        open_comments: bool,
    ) -> None:
        response_tasks: set[asyncio.Task] = set()

        async def consume_response(response) -> None:
            low_url = response.url.lower()
            if "comment" not in low_url:
                return
            try:
                content_type = (response.headers.get("content-type") or "").lower()
                if "json" not in content_type:
                    return
                payload = await response.json()
                for item in self._extract_from_json(payload, response.url):
                    self._merge(candidates, item)
            except Exception:
                return

        def on_response(response) -> None:
            task = asyncio.create_task(consume_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            self._assert_not_login(page)

            if open_comments:
                # Scroll to the discussion area and click a visible comments control if present.
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
                try:
                    await page.evaluate(
                        """
                        () => {
                          const nodes = [...document.querySelectorAll('button,a,[role="button"]')];
                          const n = nodes.find(el => /комментар/i.test((el.innerText || el.textContent || '').trim()));
                          if (n) n.click();
                        }
                        """
                    )
                    await page.wait_for_timeout(1200)
                except Exception:
                    pass

            stable = 0
            last_height = 0
            for _ in range(self.scroll_rounds):
                height = await page.evaluate("document.body.scrollHeight")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(900)
                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height == height == last_height:
                    stable += 1
                else:
                    stable = 0
                last_height = new_height
                if stable >= 3:
                    break

            for item in await self._extract_from_dom(page, url):
                self._merge(candidates, item)

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    @staticmethod
    def _assert_not_login(page: Page) -> None:
        low = (page.url or "").lower()
        if "passport.yandex" in low or "sso.passport" in low:
            raise RuntimeError(
                "Dzen просит авторизацию. Укажи DZEN_COMMENT_PROFILE_DIR с сохранённой "
                "Dzen-сессией либо используй публичный DZEN_CHANNEL_URL."
            )

    async def _extract_from_dom(self, page: Page, source_url: str) -> list[DzenComment]:
        raw: list[dict[str, Any]] = await page.evaluate(
            """
            () => {
              const selectors = [
                '[data-testid*="comment"]',
                '[data-test-id*="comment"]',
                '[class*="comment"]',
                'article'
              ];
              const nodes = [...new Set(selectors.flatMap(s => [...document.querySelectorAll(s)]))];
              const out = [];
              for (const el of nodes) {
                const all = (el.innerText || el.textContent || '').trim();
                if (!all || all.length < 12 || all.length > 3500) continue;
                const likeBits = [];
                for (const n of el.querySelectorAll('button,[role="button"],[aria-label],[title],span')) {
                  const s = [n.innerText, n.textContent, n.getAttribute('aria-label'), n.getAttribute('title')]
                    .filter(Boolean).join(' ').trim();
                  if (/нрав|лайк|like/i.test(s)) likeBits.push(s);
                }
                out.push({text: all, likeBits});
              }
              return out.slice(0, 1500);
            }
            """
        )
        result: list[DzenComment] = []
        for item in raw:
            text = self._clean_dom_text(str(item.get("text") or ""))
            if not self._valid_text(text):
                continue
            likes = 0
            for bit in item.get("likeBits") or []:
                likes = max(likes, self._number_from_like_text(str(bit)))
            # DOM fallback is useful even for zero-like comments; min-like filter is applied by worker.
            key = self._key("", text, source_url)
            result.append(DzenComment(key=key, text=text, likes=likes, source_url=source_url))
        return result

    def _extract_from_json(self, payload: Any, source_url: str) -> list[DzenComment]:
        found: list[DzenComment] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                text = self._dict_text(value)
                likes = self._dict_likes(value)
                if text and likes is not None and self._valid_text(text):
                    comment_id = self._dict_id(value)
                    author = self._dict_author(value)
                    key = self._key(comment_id, text, source_url)
                    found.append(
                        DzenComment(
                            key=key,
                            text=text,
                            likes=max(0, likes),
                            comment_id=comment_id,
                            author=author,
                            source_url=source_url,
                        )
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return found

    @staticmethod
    def _dict_text(value: dict[str, Any]) -> str:
        for key in _TEXT_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str):
                cleaned = " ".join(candidate.split())
                if 12 <= len(cleaned) <= 2500:
                    return cleaned
            elif isinstance(candidate, dict):
                for nested_key in ("text", "value", "plain", "raw"):
                    nested = candidate.get(nested_key)
                    if isinstance(nested, str):
                        cleaned = " ".join(nested.split())
                        if 12 <= len(cleaned) <= 2500:
                            return cleaned
        return ""

    @staticmethod
    def _dict_id(value: dict[str, Any]) -> str:
        for key in _ID_KEYS:
            item = value.get(key)
            if isinstance(item, (str, int)) and str(item).strip():
                return str(item).strip()
        return ""

    @staticmethod
    def _dict_author(value: dict[str, Any]) -> str:
        for key in ("authorName", "author_name", "userName", "username", "name"):
            item = value.get(key)
            if isinstance(item, str) and 1 < len(item.strip()) < 150:
                return " ".join(item.split())
        for key in ("author", "user", "owner"):
            item = value.get(key)
            if isinstance(item, dict):
                for sub in ("name", "displayName", "display_name", "username"):
                    s = item.get(sub)
                    if isinstance(s, str) and s.strip():
                        return " ".join(s.split())
        return ""

    @staticmethod
    def _dict_likes(value: dict[str, Any]) -> int | None:
        for key in _LIKE_KEYS:
            if key not in value:
                continue
            raw = value.get(key)
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                return int(raw)
            if isinstance(raw, str):
                m = re.search(r"\d+", raw.replace("\xa0", " "))
                if m:
                    return int(m.group())
            if isinstance(raw, dict):
                # reactionsCount can be {'like': 7, ...}; likes can be {'count': 7}.
                for sub in ("like", "likes", "count", "value", "total", "positive"):
                    v = raw.get(sub)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        return int(v)
        # Some APIs place the like count under a generic counter object.
        for key, raw in value.items():
            low = str(key).lower()
            if ("like" in low or "reaction" in low) and isinstance(raw, (int, float)):
                return int(raw)
        return None

    @staticmethod
    def _number_from_like_text(text: str) -> int:
        normalized = text.replace("\xa0", " ").lower()
        # Supports "12 нравится", "Нравится 12", "12 лайков", "like 12".
        nums = re.findall(r"\d+", normalized)
        return max((int(n) for n in nums), default=0)

    @staticmethod
    def _clean_dom_text(text: str) -> str:
        lines: list[str] = []
        for raw in text.splitlines():
            line = " ".join(raw.split()).strip()
            if not line:
                continue
            low = line.lower().strip(" .:—-")
            if low in _UI_WORDS:
                continue
            if re.fullmatch(r"\d+", low):
                continue
            if re.fullmatch(r"(?:ответить|поделиться|нравится|лайк(?:и|ов)?)\s*\d*", low):
                continue
            lines.append(line)
        # Prefer the meaningful content, but keep multi-line comments together.
        return " ".join(lines)[:2500].strip()

    @staticmethod
    def _valid_text(text: str) -> bool:
        text = " ".join(text.split()).strip()
        if len(text) < 12 or len(text) > 2500:
            return False
        words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text)
        return len(words) >= 3

    @staticmethod
    def _key(comment_id: str, text: str, source_url: str) -> str:
        if comment_id:
            return f"id:{comment_id}"
        digest = hashlib.sha256((text + "\n" + source_url).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _merge(target: dict[str, DzenComment], item: DzenComment) -> None:
        old = target.get(item.key)
        if old is None or item.likes > old.likes or len(item.text) > len(old.text):
            target[item.key] = item

    async def _save_debug(self, page: Page, prefix: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        try:
            await page.screenshot(path=str(self.debug_dir / f"{prefix}_{stamp}.png"), full_page=True)
        except Exception:
            pass
        try:
            html = await page.content()
            (self.debug_dir / f"{prefix}_{stamp}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass


class DzenPopularCommentWorker:
    def __init__(self, *, article_service: Any, gpt_client: Any, cfg: Any) -> None:
        self.article_service = article_service
        self.gpt = gpt_client
        self.cfg = cfg
        self.source = DzenPopularCommentSource()
        self.interval_seconds = max(
            300, int(os.getenv("DZEN_COMMENT_CHECK_MINUTES", "30")) * 60
        )
        self.min_likes = max(0, int(os.getenv("DZEN_COMMENT_MIN_LIKES", "1")))
        self.state_path = Path(
            os.getenv("DZEN_COMMENT_STATE_FILE", "data/dzen_popular_comment_state.json")
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()

        state = self._load_state()
        env_enabled = os.getenv("DZEN_POPULAR_COMMENT_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        env_preview = os.getenv("DZEN_COMMENT_PREVIEW_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
        self.enabled = bool(state.get("enabled", env_enabled))
        self.preview_enabled = bool(state.get("preview_enabled", env_preview))
        state["enabled"] = self.enabled
        state["preview_enabled"] = self.preview_enabled
        state.setdefault("used", {})
        state.setdefault("pending_previews", {})
        self._save_state(state)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        return {
            "enabled": bool(state.get("enabled", self.enabled)),
            "preview_enabled": bool(state.get("preview_enabled", self.preview_enabled)),
            "min_likes": self.min_likes,
            "interval_seconds": self.interval_seconds,
            "pending_count": len(state.get("pending_previews", {}) or {}),
        }

    def set_enabled(self, value: bool) -> bool:
        self.enabled = bool(value)
        state = self._load_state()
        state["enabled"] = self.enabled
        self._save_state(state)
        self._wake.set()
        return self.enabled

    def toggle_enabled(self) -> bool:
        return self.set_enabled(not self.enabled)

    def set_preview_enabled(self, value: bool) -> bool:
        self.preview_enabled = bool(value)
        state = self._load_state()
        state["preview_enabled"] = self.preview_enabled
        self._save_state(state)
        self._wake.set()
        return self.preview_enabled

    def toggle_preview_enabled(self) -> bool:
        return self.set_preview_enabled(not self.preview_enabled)

    async def run(self) -> None:
        log.info(
            "Dzen popular comments worker запущен: enabled=%s preview=%s check=%ss min_likes=%s source=%s",
            self.enabled,
            self.preview_enabled,
            self.interval_seconds,
            self.min_likes,
            self.source.comments_url or self.source.channel_url,
        )
        while not self._stop.is_set():
            if self.enabled:
                try:
                    result = await self.run_once()
                    log.info("Dzen popular comments cycle: %s", self._log_safe_result(result))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Dzen popular comments cycle error")
                timeout = self.interval_seconds
            else:
                timeout = 60

            self._wake.clear()
            try:
                await asyncio.wait_for(self._wait_for_stop_or_wake(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def _wait_for_stop_or_wake(self) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        done, pending = await asyncio.wait(
            {stop_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        if not self.enabled and not force:
            return {"status": "disabled"}

        # In preview mode the automatic cycle prepares the article and waits for approval.
        if self.preview_enabled:
            result = await self.create_preview(force=force)
            if result.get("status") == "preview_ready" and not force:
                await self.send_preview_to_admins(result)
            return result

        async with self._run_lock:
            selection = await self._select_candidate()
            if selection.get("status") != "candidate_ready":
                return selection

            selected: DzenComment = selection["comment_obj"]
            topic = str(selection["topic"])
            result = await self.article_service.publish_manual_topic(topic)
            status = str(result.get("status") or "")
            if status == "ok":
                self._mark_published(selected, topic, result.get("article_title"))

            return {
                "status": status or "publish_error",
                "likes": selected.likes,
                "comment": selected.text[:500],
                "topic": topic,
                "article_title": result.get("article_title"),
                "publish_result": result,
            }

    async def create_preview(self, *, force: bool = False) -> dict[str, Any]:
        if not self.enabled and not force:
            return {"status": "disabled"}

        async with self._run_lock:
            selection = await self._select_candidate()
            if selection.get("status") != "candidate_ready":
                # Manual request may re-open the already prepared preview. The automatic
                # loop must NOT resend the same preview every check interval.
                if selection.get("status") == "preview_pending" and force:
                    preview_id = str(selection.get("preview_id") or "")
                    existing = self.get_preview(preview_id)
                    if existing:
                        existing["status"] = "preview_ready"
                        existing["already_pending"] = True
                        return existing
                return selection

            selected: DzenComment = selection["comment_obj"]
            topic = str(selection["topic"])

            generated = await self.article_service.generate_manual_preview(topic)
            if generated.get("status") != "preview_ready":
                return {
                    "status": generated.get("status") or "generation_error",
                    "likes": selected.likes,
                    "comment": selected.text[:500],
                    "topic": topic,
                    "error": generated.get("error"),
                }

            import uuid
            preview_id = uuid.uuid4().hex[:12]
            preview = {
                "preview_id": preview_id,
                "comment_key": selected.key,
                "likes": selected.likes,
                "comment": selected.text[:1500],
                "source_url": selected.source_url,
                "topic": topic,
                "article_title": generated.get("article_title"),
                "full_body": generated.get("full_body"),
                "short_body": generated.get("short_body"),
                "image_path": generated.get("image_path"),
                "created_at": self._now(),
            }

            state = self._load_state()
            pending = state.setdefault("pending_previews", {})
            pending[preview_id] = preview
            used = state.setdefault("used", {})
            used[selected.key] = {
                "status": "preview_pending",
                "preview_id": preview_id,
                "likes": selected.likes,
                "text": selected.text[:1500],
                "topic": topic,
                "source_url": selected.source_url,
                "at": self._now(),
            }
            self._save_state(state)

            return {"status": "preview_ready", **preview}

    def get_preview(self, preview_id: str) -> dict[str, Any] | None:
        if not preview_id:
            return None
        state = self._load_state()
        preview = (state.get("pending_previews", {}) or {}).get(preview_id)
        if not isinstance(preview, dict):
            return None
        return dict(preview)


    def get_preview_resolution(
        self,
        preview_id: str,
    ) -> dict[str, Any] | None:
        if not preview_id:
            return None

        state = self._load_state()

        item = (
            state.get(
                "resolved_previews",
                {},
            )
            or {}
        ).get(preview_id)

        if not isinstance(
            item,
            dict,
        ):
            return None

        return dict(item)


    def _remember_preview_resolution(
        self,
        state: dict[str, Any],
        *,
        preview_id: str,
        status: str,
        actor_user_id: int | None,
        actor_name: str | None,
    ) -> None:

        resolved = state.setdefault(
            "resolved_previews",
            {},
        )

        resolved[preview_id] = {
            "status": status,
            "actor_user_id": (
                int(actor_user_id)
                if actor_user_id
                else None
            ),
            "actor_name": (
                str(actor_name or "").strip()
                or "суперадмин"
            ),
            "at": self._now(),
        }

        # Не даём служебной истории
        # бесконечно разрастаться.
        keys = list(
            resolved.keys()
        )

        if len(keys) > 300:
            for old_key in keys[:-300]:
                resolved.pop(
                    old_key,
                    None,
                )


    async def _sync_preview_admin_cards(
        self,
        preview: dict[str, Any],
        *,
        resolution: str,
        actor_name: str | None = None,
    ) -> None:
        """
        Обновляет одну и ту же карточку
        у ВСЕХ суперадминов.
        """

        refs = preview.get(
            "admin_messages",
            [],
        )

        if not isinstance(
            refs,
            list,
        ):
            refs = []

        title = str(
            preview.get(
                "article_title"
            )
            or ""
        )

        short_body = str(
            preview.get(
                "short_body"
            )
            or ""
        )

        actor = (
            str(actor_name or "").strip()
            or "суперадмин"
        )

        if resolution == "published":
            status_text = (
                "✅ ОПУБЛИКОВАНО\n"
                f"Решение принял: {actor}"
            )

        elif resolution == "rejected":
            status_text = (
                "❌ ОТМЕНЕНО\n"
                f"Решение принял: {actor}"
            )

        else:
            status_text = (
                "ℹ️ Статья уже обработана."
            )

        text = (
            "📱 SHORT ДЛЯ TELEGRAM\n\n"
            f"{title}\n\n"
            f"{short_body}\n\n"
            "──────────────\n"
            f"{status_text}"
        )

        for ref in refs:
            if not isinstance(
                ref,
                dict,
            ):
                continue

            try:
                chat_id = int(
                    ref["chat_id"]
                )

                message_id = int(
                    ref["message_id"]
                )

            except Exception:
                continue

            try:
                await (
                    self.article_service
                    .bot
                    .edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=None,
                    )
                )

            except Exception as exc:
                # Например, сообщение было
                # вручную удалено администратором.
                log.warning(
                    "Не удалось синхронизировать "
                    "preview=%s chat=%s msg=%s: %s",
                    preview.get(
                        "preview_id"
                    ),
                    chat_id,
                    message_id,
                    exc,
                )



    async def publish_preview(
        self,
        preview_id: str,
        *,
        actor_user_id: int | None = None,
        actor_name: str | None = None,
    ) -> dict[str, Any]:

        async with self._run_lock:

            state = self._load_state()

            pending = state.setdefault(
                "pending_previews",
                {},
            )

            preview = pending.get(
                preview_id
            )

            if not isinstance(
                preview,
                dict,
            ):
                resolved = (
                    self.get_preview_resolution(
                        preview_id
                    )
                )

                return {
                    "status": "already_processed",
                    "preview_id": preview_id,
                    "resolution": (
                        resolved.get(
                            "status"
                        )
                        if resolved
                        else None
                    ),
                    "actor_name": (
                        resolved.get(
                            "actor_name"
                        )
                        if resolved
                        else None
                    ),
                }

            # Копия нужна, потому что ниже
            # preview будет удалён из pending.
            preview = dict(
                preview
            )

            result = (
                await self.article_service
                .publish_prepared_manual(
                    topic_title=str(
                        preview.get(
                            "topic"
                        )
                        or ""
                    ),
                    title=str(
                        preview.get(
                            "article_title"
                        )
                        or ""
                    ),
                    full_body=str(
                        preview.get(
                            "full_body"
                        )
                        or ""
                    ),
                    short_body=str(
                        preview.get(
                            "short_body"
                        )
                        or ""
                    ),
                    image_path=str(
                        preview.get(
                            "image_path"
                        )
                        or ""
                    ),
                    trigger_type=(
                        "dzen_comment_approved"
                    ),
                )
            )

            status = str(
                result.get(
                    "status"
                )
                or ""
            )

            if status == "ok":

                state = self._load_state()

                pending = state.setdefault(
                    "pending_previews",
                    {},
                )

                pending.pop(
                    preview_id,
                    None,
                )

                used = state.setdefault(
                    "used",
                    {},
                )

                key = str(
                    preview.get(
                        "comment_key"
                    )
                    or ""
                )

                if key:
                    used[key] = {
                        "status": "published",
                        "likes": int(
                            preview.get(
                                "likes"
                            )
                            or 0
                        ),
                        "text": str(
                            preview.get(
                                "comment"
                            )
                            or ""
                        )[:1500],
                        "topic": str(
                            preview.get(
                                "topic"
                            )
                            or ""
                        ),
                        "source_url": str(
                            preview.get(
                                "source_url"
                            )
                            or ""
                        ),
                        "article_title": str(
                            preview.get(
                                "article_title"
                            )
                            or ""
                        ),
                        "at": self._now(),
                    }

                    state[
                        "last_published_key"
                    ] = key

                    state[
                        "last_published_at"
                    ] = self._now()

                self._remember_preview_resolution(
                    state,
                    preview_id=preview_id,
                    status="published",
                    actor_user_id=(
                        actor_user_id
                    ),
                    actor_name=actor_name,
                )

                self._save_state(
                    state
                )

                # После успешной публикации
                # убираем кнопки сразу У ВСЕХ.
                await (
                    self._sync_preview_admin_cards(
                        preview,
                        resolution="published",
                        actor_name=actor_name,
                    )
                )

            return {
                "status": (
                    status
                    or "publish_error"
                ),
                "preview_id": preview_id,
                "topic": preview.get(
                    "topic"
                ),
                "article_title": (
                    preview.get(
                        "article_title"
                    )
                ),
                "publish_result": result,
            }


    async def cancel_preview(
        self,
        preview_id: str,
        *,
        actor_user_id: int | None = None,
        actor_name: str | None = None,
    ) -> dict[str, Any]:

        async with self._run_lock:

            state = self._load_state()

            pending = state.setdefault(
                "pending_previews",
                {},
            )

            preview = pending.pop(
                preview_id,
                None,
            )

            if not isinstance(
                preview,
                dict,
            ):
                resolved = (
                    self.get_preview_resolution(
                        preview_id
                    )
                )

                return {
                    "status": "already_processed",
                    "preview_id": preview_id,
                    "resolution": (
                        resolved.get(
                            "status"
                        )
                        if resolved
                        else None
                    ),
                    "actor_name": (
                        resolved.get(
                            "actor_name"
                        )
                        if resolved
                        else None
                    ),
                }

            key = str(
                preview.get(
                    "comment_key"
                )
                or ""
            )

            used = state.setdefault(
                "used",
                {},
            )

            if key:
                used[key] = {
                    "status": "rejected",
                    "likes": int(
                        preview.get(
                            "likes"
                        )
                        or 0
                    ),
                    "text": str(
                        preview.get(
                            "comment"
                        )
                        or ""
                    )[:1500],
                    "topic": str(
                        preview.get(
                            "topic"
                        )
                        or ""
                    ),
                    "source_url": str(
                        preview.get(
                            "source_url"
                        )
                        or ""
                    ),
                    "at": self._now(),
                }

            self._remember_preview_resolution(
                state,
                preview_id=preview_id,
                status="rejected",
                actor_user_id=actor_user_id,
                actor_name=actor_name,
            )

            self._save_state(
                state
            )

            await (
                self._sync_preview_admin_cards(
                    dict(preview),
                    resolution="rejected",
                    actor_name=actor_name,
                )
            )

            image_path = str(
                preview.get(
                    "image_path"
                )
                or ""
            )

            if image_path:
                try:
                    Path(
                        image_path
                    ).unlink(
                        missing_ok=True
                    )

                except Exception:
                    log.exception(
                        "Не удалось удалить "
                        "изображение отменённого "
                        "preview"
                    )

            return {
                "status": "cancelled",
                "preview_id": preview_id,
            }


    async def send_preview_to_admins(
        self,
        preview: dict[str, Any],
        *,
        chat_ids: list[int] | None = None,
    ) -> None:

        from aiogram.types import (
            BufferedInputFile,
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )

        preview_id = str(
            preview.get(
                "preview_id"
            )
            or ""
        )

        if not preview_id:
            return

        if chat_ids is None:
            chat_ids = [
                int(x)
                for x in self.cfg.admin_ids
            ]

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Опубликовать",
                        callback_data=(
                            "dzenpc:publish:"
                            f"{preview_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="❌ Отменить",
                        callback_data=(
                            "dzenpc:cancel:"
                            f"{preview_id}"
                        ),
                    ),
                ]
            ]
        )

        comment = str(
            preview.get(
                "comment"
            )
            or ""
        )

        topic = str(
            preview.get(
                "topic"
            )
            or ""
        )

        title = str(
            preview.get(
                "article_title"
            )
            or ""
        )

        full_body = str(
            preview.get(
                "full_body"
            )
            or ""
        )

        short_body = str(
            preview.get(
                "short_body"
            )
            or ""
        )

        image_path = str(
            preview.get(
                "image_path"
            )
            or ""
        )

        likes = int(
            preview.get(
                "likes"
            )
            or 0
        )

        # Пока карточки рассылаются,
        # Publish/Cancel ждут тот же lock.
        # Поэтому ни один админ не сможет
        # обработать preview посреди рассылки.
        async with self._run_lock:

            state = self._load_state()

            pending = state.setdefault(
                "pending_previews",
                {},
            )

            stored = pending.get(
                preview_id
            )

            if not isinstance(
                stored,
                dict,
            ):
                return

            refs = stored.get(
                "admin_messages",
                [],
            )

            if not isinstance(
                refs,
                list,
            ):
                refs = []

            for chat_id in chat_ids:

                try:
                    await (
                        self.article_service
                        .bot
                        .send_message(
                            chat_id,
                            "🔥 ПРЕДПРОСМОТР ПО "
                            "ПОПУЛЯРНОМУ КОММЕНТАРИЮ"
                            "\n\n"
                            f"❤️ Лайков: {likes}\n"
                            "💬 Комментарий:\n"
                            f"{comment}\n\n"
                            "📝 Тема:\n"
                            f"{topic}",
                        )
                    )

                    if image_path:
                        try:
                            image_bytes = (
                                Path(
                                    image_path
                                ).read_bytes()
                            )

                            await (
                                self.article_service
                                .bot
                                .send_photo(
                                    chat_id,
                                    BufferedInputFile(
                                        image_bytes,
                                        filename=(
                                            "preview_"
                                            f"{preview_id}"
                                            ".jpg"
                                        ),
                                    ),
                                    caption=(
                                        "🖼 Картинка статьи"
                                    ),
                                )
                            )

                        except Exception:
                            log.exception(
                                "Не удалось отправить "
                                "preview image админу"
                            )

                    await (
                        self.article_service
                        .bot
                        .send_message(
                            chat_id,
                            "📄 LONG ДЛЯ ДЗЕНА"
                            "\n\n"
                            f"{title}\n\n"
                            f"{full_body}",
                        )
                    )

                    short_message = await (
                        self.article_service
                        .bot
                        .send_message(
                            chat_id,
                            "📱 SHORT ДЛЯ TELEGRAM"
                            "\n\n"
                            f"{title}\n\n"
                            f"{short_body}",
                            reply_markup=kb,
                        )
                    )

                    ref = {
                        "chat_id": int(
                            chat_id
                        ),
                        "message_id": int(
                            short_message.message_id
                        ),
                    }

                    if ref not in refs:
                        refs.append(
                            ref
                        )

                except Exception:
                    log.exception(
                        "Не удалось отправить "
                        "preview админу %s",
                        chat_id,
                    )

            stored[
                "admin_messages"
            ] = refs[-100:]

            pending[
                preview_id
            ] = stored

            self._save_state(
                state
            )

    async def _select_candidate(self) -> dict[str, Any]:
        ranked = await self.source.ranked_comments()
        if not ranked:
            return {"status": "no_comments"}

        state = self._load_state()
        used: dict[str, Any] = state.setdefault("used", {})
        eligible = [c for c in ranked if c.likes >= self.min_likes]
        if not eligible:
            return {
                "status": "no_liked_comments",
                "max_likes": ranked[0].likes if ranked else 0,
            }

        published_skipped = 0

        for candidate in eligible:
            previous = used.get(candidate.key)
            if isinstance(previous, dict):
                previous_status = str(previous.get("status") or "").strip().lower()

                if previous_status == "skipped":
                    continue

                # Уже опубликованный комментарий больше не блокирует рейтинг.
                # Просто идём к следующему по количеству лайков.
                if previous_status == "published":
                    published_skipped += 1
                    continue

                if previous_status == "rejected":
                    return {
                        "status": "top_rejected",
                        "likes": candidate.likes,
                        "comment": candidate.text[:300],
                    }

                if previous_status == "preview_pending":
                    return {
                        "status": "preview_pending",
                        "preview_id": previous.get("preview_id"),
                        "likes": candidate.likes,
                        "comment": candidate.text[:300],
                    }

            topic = await self._topic_from_comment(candidate.text)
            if topic:
                return {
                    "status": "candidate_ready",
                    "comment_obj": candidate,
                    "topic": topic,
                }

            used[candidate.key] = {
                "status": "skipped",
                "likes": candidate.likes,
                "text": candidate.text[:1000],
                "at": self._now(),
            }
            self._save_state(state)

        if published_skipped:
            return {
                "status": "no_fresh_comments",
                "used_count": published_skipped,
            }

        return {"status": "no_article_worthy_comments"}

    def _mark_published(self, selected: DzenComment, topic: str, article_title: Any) -> None:
        state = self._load_state()
        used = state.setdefault("used", {})
        used[selected.key] = {
            "status": "published",
            "likes": selected.likes,
            "text": selected.text[:1500],
            "topic": topic,
            "source_url": selected.source_url,
            "article_title": article_title,
            "at": self._now(),
        }
        state["last_published_key"] = selected.key
        state["last_published_at"] = self._now()
        self._save_state(state)

    async def _topic_from_comment(self, text: str) -> str:
        prompt = "КОММЕНТАРИЙ:\n" + text[:2200]
        try:
            auth = await self.gpt.auth_header()
            raw = await asyncio.to_thread(
                self.gpt._complete_sync,
                auth,
                _TOPIC_SYSTEM_PROMPT,
                prompt,
            )
            topic = " ".join(str(raw).split()).strip(" \"'«»")
            topic = re.sub(r"^(?:ТЕМА\s*:\s*)", "", topic, flags=re.I).strip()
            if not topic or topic.upper() == "SKIP":
                return ""
            return topic[:280].rstrip(" .,:;—-")
        except Exception:
            log.exception("Не удалось сформулировать тему из Dzen-комментария")
            cleaned = " ".join(text.split()).strip()
            return cleaned[:240] if len(cleaned) >= 20 else ""

    def _load_state(self) -> dict[str, Any]:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            log.exception("Не удалось прочитать Dzen comment state")
        return {"used": {}, "pending_previews": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def _log_safe_result(result: dict[str, Any]) -> dict[str, Any]:
        safe = dict(result)
        safe.pop("full_body", None)
        safe.pop("short_body", None)
        return safe

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
