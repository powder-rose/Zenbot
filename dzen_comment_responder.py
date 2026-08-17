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
from urllib.parse import urljoin

from playwright.async_api import Locator, Page, async_playwright

from dzen_browser_lock import DZEN_BROWSER_LOCK

log = logging.getLogger(__name__)

DEFAULT_REPLY_PROMPT = """
Ты отвечаешь на комментарии читателей от имени экспертного блога компании.
Пиши естественно, по-человечески, без канцелярита и без ощущения ответа нейросети.
Ответ должен быть полезным и связанным именно с комментарием. Не выдумывай факты,
законы, цифры и обстоятельства, которых нет в исходных данных.

Правила:
1. Обычно 1–3 коротких абзаца, максимум около 700 символов.
2. На вопрос отвечай по существу. С возражением можно спокойно не согласиться и объяснить почему.
3. На нормальную благодарность можно ответить коротко и доброжелательно.
4. На спам, бессвязный текст, явную провокацию без смысла или комментарий, на который нельзя
   корректно ответить, верни ровно одно слово: SKIP
5. Если это естественно по смыслу, можно мягко пригласить посмотреть другие практические
   материалы: https://boykovgroup.ru/blog . Не вставляй ссылку в каждый ответ насильно.
6. Не пиши служебных пояснений, JSON, Markdown-код, «Ответ:» и т.п.
Верни только готовый текст ответа либо SKIP.
""".strip()

_REFUSAL_MARKERS = (
    "я не могу обсуждать",
    "я не могу ответить",
    "не могу обсуждать эту тему",
    "не могу помочь с этим",
    "я не могу помочь",
    "не могу выполнить этот запрос",
    "i can't help",
    "i cannot help",
)


@dataclass(slots=True)
class ResponderComment:
    key: str
    comment_id: str
    text: str
    author: str
    article_title: str
    href: str


class DzenCommentResponderWorker:
    def __init__(self, *, gpt_client: Any, cfg: Any) -> None:
        self.gpt = gpt_client
        self.cfg = cfg
        self.comments_url = (
            os.getenv("DZEN_RESPONDER_COMMENTS_URL", "").strip()
            or os.getenv("DZEN_COMMENTS_URL", "").strip()
        )
        self.profile_dir = (
            os.getenv("DZEN_RESPONDER_PROFILE_DIR", "").strip()
            or os.getenv("DZEN_COMMENT_PROFILE_DIR", "").strip()
            or os.getenv("DZEN_PROFILE_DIR", "data/dzen-profile").strip()
        )
        self.headless = self._env_bool(
            "DZEN_RESPONDER_HEADLESS",
            self._env_bool("DZEN_COMMENT_HEADLESS", True),
        )
        self.interval_seconds = max(
            300,
            int(os.getenv("DZEN_RESPONDER_CHECK_MINUTES", "10")) * 60,
        )
        self.max_per_cycle = max(
            1,
            min(25, int(os.getenv("DZEN_RESPONDER_MAX_PER_CYCLE", "5"))),
        )
        self.max_reply_chars = max(
            120,
            min(1500, int(os.getenv("DZEN_RESPONDER_MAX_REPLY_CHARS", "700"))),
        )
        self.scroll_rounds = max(
            2,
            min(30, int(os.getenv("DZEN_RESPONDER_SCROLLS", "8"))),
        )
        self.state_path = Path(
            os.getenv(
                "DZEN_RESPONDER_STATE_FILE",
                "data/dzen_comment_responder_state.json",
            )
        )
        self.debug_dir = Path(
            os.getenv(
                "DZEN_RESPONDER_DEBUG_DIR",
                "data/dzen_comment_responder_debug",
            )
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        own_names_raw = os.getenv("DZEN_RESPONDER_OWN_NAMES", "")
        self.own_names = {
            " ".join(value.lower().split())
            for value in re.split(r"[,;\n]+", own_names_raw)
            if value.strip()
        }

        state = self._load_state()
        env_enabled = self._env_bool("DZEN_RESPONDER_ENABLED", False)
        # Совместимость со старым Dzen Comment Responder: если есть общий DRY_RUN,
        # используем его как fallback, но отдельная переменная имеет приоритет.
        if "DZEN_RESPONDER_DRY_RUN" in os.environ:
            env_dry_run = self._env_bool("DZEN_RESPONDER_DRY_RUN", False)
        else:
            env_dry_run = self._env_bool("DRY_RUN", False)

        self.enabled = bool(state.get("enabled", env_enabled))
        self.dry_run = bool(state.get("dry_run", env_dry_run))
        state["enabled"] = self.enabled
        state["dry_run"] = self.dry_run
        state.setdefault("processed", {})
        state.setdefault("stats", {})
        self._save_state(state)

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return bool(default)
        return value.strip().lower() in {"1", "true", "yes", "on", "да"}

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        stats = state.get("stats", {}) or {}
        processed = state.get("processed", {}) or {}
        return {
            "enabled": bool(state.get("enabled", self.enabled)),
            "dry_run": bool(state.get("dry_run", self.dry_run)),
            "interval_seconds": self.interval_seconds,
            "max_per_cycle": self.max_per_cycle,
            "processed_count": len(processed),
            "replied": int(stats.get("replied", 0) or 0),
            "skipped": int(stats.get("skipped", 0) or 0),
            "errors": int(stats.get("errors", 0) or 0),
            "dry_runs": int(stats.get("dry_runs", 0) or 0),
            "last_cycle_at": state.get("last_cycle_at"),
            "last_reply_at": state.get("last_reply_at"),
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

    def set_dry_run(self, value: bool) -> bool:
        self.dry_run = bool(value)
        state = self._load_state()
        state["dry_run"] = self.dry_run
        self._save_state(state)
        self._wake.set()
        return self.dry_run

    def toggle_dry_run(self) -> bool:
        return self.set_dry_run(not self.dry_run)

    async def run(self) -> None:
        log.info(
            "Dzen responder worker запущен: enabled=%s dry_run=%s check=%ss max_per_cycle=%s",
            self.enabled,
            self.dry_run,
            self.interval_seconds,
            self.max_per_cycle,
        )
        while not self._stop.is_set():
            if self.enabled:
                try:
                    result = await self.run_once()
                    log.info("Dzen responder cycle: %s", result)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Dzen responder cycle error")
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
            {stop_task, wake_task},
            return_when=asyncio.FIRST_COMPLETED,
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
        if not self.comments_url:
            return {
                "status": "config_error",
                "error": "Не задан DZEN_COMMENTS_URL/DZEN_RESPONDER_COMMENTS_URL",
            }

        async with self._run_lock:
            comments = await self._scan_comments()
            state = self._load_state()
            processed: dict[str, Any] = state.setdefault("processed", {})

            fresh = [item for item in comments if item.key not in processed]
            if not fresh:
                state["last_cycle_at"] = self._now()
                self._save_state(state)
                return {
                    "status": "no_new_comments",
                    "found": len(comments),
                }

            selected = fresh[: self.max_per_cycle]
            plans: list[tuple[ResponderComment, str]] = []
            skipped = 0
            ai_errors = 0

            for item in selected:
                if self._is_own_comment(item):
                    self._mark_processed(
                        state,
                        item,
                        status="skipped_own",
                        reply="",
                    )
                    skipped += 1
                    continue

                try:
                    reply = await self._generate_reply(item)
                except Exception as exc:
                    ai_errors += 1
                    log.exception("Dzen responder: ошибка YandexGPT для %s", item.key)
                    self._bump_stat(state, "errors")
                    # Не помечаем processed: временная ошибка сможет повториться позже.
                    continue

                if not reply:
                    self._mark_processed(
                        state,
                        item,
                        status="skipped_ai",
                        reply="",
                    )
                    skipped += 1
                    self._bump_stat(state, "skipped")
                    continue

                plans.append((item, reply))

            if self.dry_run:
                previews = []
                for item, reply in plans:
                    previews.append(
                        {
                            "comment_id": item.comment_id,
                            "author": item.author,
                            "comment": item.text[:300],
                            "reply": reply,
                        }
                    )
                self._bump_stat(state, "dry_runs", len(plans))
                state["last_cycle_at"] = self._now()
                self._save_state(state)
                return {
                    "status": "dry_run",
                    "found": len(comments),
                    "new": len(fresh),
                    "prepared": len(plans),
                    "skipped": skipped,
                    "ai_errors": ai_errors,
                    "previews": previews,
                }

            posted = 0
            post_errors = 0
            if plans:
                results = await self._post_replies(plans)
                for item, reply, ok, error, verified in results:
                    if ok:
                        posted += 1
                        self._mark_processed(
                            state,
                            item,
                            status="replied" if verified else "replied_unverified",
                            reply=reply,
                        )
                        self._bump_stat(state, "replied")
                        state["last_reply_at"] = self._now()
                    else:
                        post_errors += 1
                        self._bump_stat(state, "errors")
                        log.error(
                            "Dzen responder: не удалось ответить comment=%s: %s",
                            item.comment_id or item.key,
                            error,
                        )
                        # Если клик отправки не состоялся, не помечаем processed — будет retry.

            state["last_cycle_at"] = self._now()
            self._save_state(state)
            return {
                "status": "ok",
                "found": len(comments),
                "new": len(fresh),
                "replied": posted,
                "skipped": skipped,
                "errors": ai_errors + post_errors,
            }

    async def _scan_comments(self) -> list[ResponderComment]:
        profile = Path(self.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 1000},
                    locale="ru-RU",
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(
                        self.comments_url,
                        wait_until="domcontentloaded",
                        timeout=90000,
                    )
                    await page.wait_for_timeout(2500)
                    self._assert_authorized(page)

                    for _ in range(self.scroll_rounds):
                        before = await page.evaluate("document.body.scrollHeight")
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(800)
                        after = await page.evaluate("document.body.scrollHeight")
                        if after == before:
                            break

                    raw: list[dict[str, Any]] = await page.evaluate(
                        """
                        () => {
                          const sels = [
                            'a[aria-label="Комментарий"][href*="#comment_"]',
                            'a[href*="comments_data"][href*="#comment_"]',
                            'a[href*="#comment_"]'
                          ];
                          const anchors = [...new Set(sels.flatMap(s => [...document.querySelectorAll(s)]))];
                          return anchors.map(a => {
                            const textNode = a.querySelector(
                              'p[class*="comment__text"], [class*="comment__text"], p'
                            );
                            const authorNode = a.querySelector(
                              '[class*="authorName"], [class*="author-name"], [class*="comment__author"]'
                            );
                            const titled = [...a.querySelectorAll('[title]')]
                              .map(n => n.getAttribute('title') || '')
                              .filter(Boolean)
                              .sort((x,y) => y.length - x.length);
                            return {
                              href: a.href || a.getAttribute('href') || '',
                              text: (textNode?.innerText || textNode?.textContent || '').trim(),
                              author: (authorNode?.innerText || authorNode?.textContent || '').trim(),
                              articleTitle: titled[0] || ''
                            };
                          });
                        }
                        """
                    )
                except Exception:
                    await self._save_debug(page, "scan_error")
                    raise
                finally:
                    await context.close()

        result: list[ResponderComment] = []
        seen: set[str] = set()
        for item in raw:
            href = str(item.get("href") or "").strip()
            text = " ".join(str(item.get("text") or "").split()).strip()
            author = " ".join(str(item.get("author") or "").split()).strip()
            article_title = " ".join(str(item.get("articleTitle") or "").split()).strip()
            if not href or not text or len(text) < 2:
                continue
            href = urljoin(self.comments_url, href)
            # В Author Studio ссылки на ещё не отвеченные комментарии содержат
            # comments_data=n_reply. Если Dzen явно указал другой статус, не отвечаем
            # повторно. Если параметра нет совсем, оставляем fallback для совместимости.
            low_href = href.lower()
            if "comments_data=" in low_href and "comments_data=n_reply" not in low_href:
                continue
            m = re.search(r"#comment_([A-Za-z0-9_-]+)", href)
            comment_id = m.group(1) if m else ""
            if comment_id:
                key = f"id:{comment_id}"
            else:
                digest = hashlib.sha256((href + "\n" + text).encode("utf-8")).hexdigest()
                key = f"sha256:{digest}"
            if key in seen:
                continue
            seen.add(key)
            result.append(
                ResponderComment(
                    key=key,
                    comment_id=comment_id,
                    text=text[:2500],
                    author=author[:150],
                    article_title=article_title[:500],
                    href=href,
                )
            )
        return result

    async def _generate_reply(self, item: ResponderComment) -> str:
        prompt = (
            f"ЗАГОЛОВОК МАТЕРИАЛА:\n{item.article_title or 'не указан'}\n\n"
            f"АВТОР КОММЕНТАРИЯ:\n{item.author or 'не указан'}\n\n"
            f"КОММЕНТАРИЙ:\n{item.text}"
        )
        auth = await self.gpt.auth_header()
        raw = await asyncio.to_thread(
            self.gpt._complete_sync,
            auth,
            DEFAULT_REPLY_PROMPT,
            prompt,
        )
        reply = self._clean_reply(str(raw or ""))
        if not reply:
            return ""
        return self._truncate_reply(reply)

    def _clean_reply(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```(?:json|text)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        text = re.sub(r"^\s*(?:ответ|reply)\s*:\s*", "", text, flags=re.I)
        text = text.strip(" \t\n\r\"'«»")
        if not text:
            return ""
        low = " ".join(text.lower().split())
        if low == "skip" or low.startswith("skip "):
            return ""
        if any(marker in low for marker in _REFUSAL_MARKERS):
            return ""
        # Старый responder ожидал JSON и падал на обычном тексте. Новый наоборот:
        # если GPT зачем-то вернул JSON, аккуратно достанем reply/text/answer.
        if text.startswith("{") and text.endswith("}"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    for key in ("reply", "answer", "text", "response"):
                        value = data.get(key)
                        if isinstance(value, str) and value.strip():
                            text = value.strip()
                            break
            except Exception:
                pass
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()

    def _truncate_reply(self, text: str) -> str:
        if len(text) <= self.max_reply_chars:
            return text
        cut = text[: self.max_reply_chars].rstrip()
        end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if end >= int(self.max_reply_chars * 0.55):
            cut = cut[: end + 1]
        return cut.rstrip()

    async def _post_replies(
        self,
        plans: list[tuple[ResponderComment, str]],
    ) -> list[tuple[ResponderComment, str, bool, str, bool]]:
        profile = Path(self.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        results: list[tuple[ResponderComment, str, bool, str, bool]] = []

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 1000},
                    locale="ru-RU",
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    for item, reply in plans:
                        try:
                            verified = await self._post_one(page, item, reply)
                            results.append((item, reply, True, "", verified))
                        except Exception as exc:
                            await self._save_debug(
                                page,
                                f"reply_error_{item.comment_id or 'unknown'}",
                            )
                            results.append((item, reply, False, str(exc), False))
                finally:
                    await context.close()
        return results

    async def _post_one(self, page: Page, item: ResponderComment, reply: str) -> bool:
        await page.goto(item.href, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        self._assert_authorized(page)

        target = await self._find_comment_element(page, item)
        if target is not None:
            try:
                await target.scroll_into_view_if_needed()
            except Exception:
                pass

        reply_button = await self._find_reply_button(page, target)
        if reply_button is None:
            raise RuntimeError("Не найдена кнопка «Ответить» у комментария")
        await reply_button.click(timeout=7000, force=True)
        await page.wait_for_timeout(600)

        composer = await self._find_composer(page, target)
        if composer is None:
            raise RuntimeError("После «Ответить» не найдено поле ввода ответа")

        await self._fill_composer(page, composer, reply)
        await page.wait_for_timeout(250)

        send_button = await self._find_send_button(page, composer)
        if send_button is not None:
            await send_button.click(timeout=7000, force=True)
        else:
            # Dzen в некоторых вариантах формы отправляет ответ Ctrl+Enter.
            await composer.focus()
            await page.keyboard.press("Control+Enter")

        await page.wait_for_timeout(1600)

        # Если текст уже виден — подтверждаем. Если нет, сам успешный клик отправки
        # всё равно считаем отправкой и помечаем unverified, чтобы не задвоить ответ.
        needle = " ".join(reply.split())[:80]
        if len(needle) >= 12:
            try:
                visible = page.get_by_text(re.compile(re.escape(needle[:50]), re.I)).last
                if await visible.count() and await visible.is_visible():
                    return True
            except Exception:
                pass
        return False

    async def _find_comment_element(
        self,
        page: Page,
        item: ResponderComment,
    ) -> Locator | None:
        selectors: list[str] = []
        if item.comment_id:
            cid = item.comment_id.replace('"', '\\"')
            selectors.extend(
                [
                    f'#comment_{cid}',
                    f'[id="comment_{cid}"]',
                    f'[data-comment-id="{cid}"]',
                    f'[data-id="{cid}"]',
                    f'[id*="comment_{cid}"]',
                ]
            )
        for selector in selectors:
            try:
                loc = page.locator(selector).last
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue

        sample = " ".join(item.text.split())[:100]
        if sample:
            try:
                text_loc = page.get_by_text(re.compile(re.escape(sample[:60]), re.I)).first
                if await text_loc.count():
                    wrapper = text_loc.locator(
                        "xpath=ancestor::*[contains(@class,'comment') or @data-comment-id][1]"
                    )
                    if await wrapper.count():
                        return wrapper
                    return text_loc
            except Exception:
                pass
        return None

    async def _find_reply_button(
        self,
        page: Page,
        target: Locator | None,
    ) -> Locator | None:
        scopes = [target, page] if target is not None else [page]
        for scope in scopes:
            if scope is None:
                continue
            candidates = [
                scope.get_by_role("button", name=re.compile(r"^ответить$|^reply$", re.I)),
                scope.get_by_text(re.compile(r"^ответить$|^reply$", re.I)),
                scope.locator('[aria-label*="ответ" i], [title*="ответ" i]'),
            ]
            found = await self._first_visible(candidates, prefer_last=False)
            if found is not None:
                return found
        return None

    async def _find_composer(
        self,
        page: Page,
        target: Locator | None,
    ) -> Locator | None:
        candidates = [
            page.get_by_placeholder(re.compile(r"ответ|комментар|reply|comment", re.I)),
            page.locator('textarea:visible'),
            page.locator('[contenteditable="true"]:visible'),
        ]
        return await self._first_visible(candidates, prefer_last=True)

    async def _fill_composer(self, page: Page, composer: Locator, reply: str) -> None:
        tag = ""
        try:
            tag = (await composer.evaluate("el => el.tagName.toLowerCase()")) or ""
        except Exception:
            pass
        if tag in {"textarea", "input"}:
            await composer.fill(reply)
            return
        await composer.click(timeout=5000, force=True)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(reply)

    async def _find_send_button(
        self,
        page: Page,
        composer: Locator,
    ) -> Locator | None:
        form = composer.locator("xpath=ancestor::form[1]")
        scopes: list[Any] = []
        try:
            if await form.count():
                scopes.append(form)
        except Exception:
            pass
        scopes.append(page)

        for scope in scopes:
            candidates = [
                scope.get_by_role(
                    "button",
                    name=re.compile(r"отправить|опубликовать|send|submit", re.I),
                ),
                scope.locator('button[type="submit"]'),
                scope.locator('[aria-label*="отправ" i], [title*="отправ" i]'),
            ]
            found = await self._first_visible(candidates, prefer_last=True)
            if found is not None:
                return found
        return None

    @staticmethod
    async def _first_visible(
        candidates: list[Locator],
        *,
        prefer_last: bool,
    ) -> Locator | None:
        for candidate in candidates:
            try:
                count = await candidate.count()
            except Exception:
                continue
            indexes = range(count - 1, -1, -1) if prefer_last else range(count)
            for index in indexes:
                try:
                    loc = candidate.nth(index)
                    if await loc.is_visible():
                        return loc
                except Exception:
                    continue
        return None

    def _is_own_comment(self, item: ResponderComment) -> bool:
        if not self.own_names or not item.author:
            return False
        author = " ".join(item.author.lower().split())
        return author in self.own_names

    @staticmethod
    def _assert_authorized(page: Page) -> None:
        low = (page.url or "").lower()
        if "passport.yandex" in low or "sso.passport" in low:
            raise RuntimeError(
                "Dzen просит авторизацию. Проверь DZEN_COMMENT_PROFILE_DIR / DZEN_RESPONDER_PROFILE_DIR."
            )

    async def _save_debug(self, page: Page, prefix: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        try:
            await page.screenshot(
                path=str(self.debug_dir / f"{prefix}_{stamp}.png"),
                full_page=True,
            )
        except Exception:
            pass
        try:
            html = await page.content()
            (self.debug_dir / f"{prefix}_{stamp}.html").write_text(
                html,
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_state(self) -> dict[str, Any]:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            log.exception("Не удалось прочитать Dzen responder state")
        return {"processed": {}, "stats": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

    def _mark_processed(
        self,
        state: dict[str, Any],
        item: ResponderComment,
        *,
        status: str,
        reply: str,
    ) -> None:
        processed = state.setdefault("processed", {})
        processed[item.key] = {
            "status": status,
            "comment_id": item.comment_id,
            "author": item.author,
            "comment": item.text[:1500],
            "article_title": item.article_title,
            "href": item.href,
            "reply": reply[:1500],
            "at": self._now(),
        }

    @staticmethod
    def _bump_stat(state: dict[str, Any], key: str, amount: int = 1) -> None:
        stats = state.setdefault("stats", {})
        stats[key] = int(stats.get(key, 0) or 0) + int(amount)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
