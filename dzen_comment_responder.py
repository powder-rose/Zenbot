from __future__ import annotations

from yandex_gpt import ContentBlockedError

from ai_usage import usage_context

import asyncio
import hashlib
import json
import logging
import os

import tenant_db
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

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
5. По умолчанию не добавляй ссылки, URL, адреса сайтов и призывы перейти на сайт или в блог.
   Ссылку добавляй только в том случае, если это прямо указано пользователем в текущем промпте.
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
    created_raw: str


class DzenCommentResponderWorker:
    def __init__(
        self,
        *,
        gpt_client: Any,
        cfg: Any,
        tenant_user_id: int | None = None,
        comments_url: str | None = None,
        profile_dir: str | None = None,
        state_file: str | None = None,
        debug_dir: str | None = None,
    ) -> None:
        self.gpt = gpt_client
        self.cfg = cfg

        self.tenant_user_id = (
            int(tenant_user_id)
            if tenant_user_id is not None
            else None
        )

        # Для tenant-worker тариф ограничен 3 подтверждёнными
        # ответами за календарные сутки.
        # Старый общий worker остаётся без этого ограничения.
        self.daily_reply_limit = (
            10
            if self.tenant_user_id is not None
            else None
        )

        self.comments_url = (
            str(comments_url or "").strip()
            or os.getenv(
                "DZEN_RESPONDER_COMMENTS_URL",
                "",
            ).strip()
            or os.getenv(
                "DZEN_COMMENTS_URL",
                "",
            ).strip()
        )

        self.profile_dir = (
            str(profile_dir or "").strip()
            or os.getenv(
                "DZEN_RESPONDER_PROFILE_DIR",
                "",
            ).strip()
            or os.getenv(
                "DZEN_COMMENT_PROFILE_DIR",
                "",
            ).strip()
            or os.getenv(
                "DZEN_PROFILE_DIR",
                "data/dzen-profile",
            ).strip()
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
        self.week_scroll_rounds = max(
            self.scroll_rounds,
            min(120, int(os.getenv("DZEN_RESPONDER_WEEK_SCROLLS", "60"))),
        )
        self.week_days = max(
            1,
            min(30, int(os.getenv("DZEN_RESPONDER_WEEK_DAYS", "7"))),
        )
        self.reply_timeout_seconds = max(
            15,
            min(90, int(os.getenv("DZEN_RESPONDER_REPLY_TIMEOUT_SECONDS", "45"))),
        )
        self.reply_concurrency = max(
            1,
            min(5, int(os.getenv("DZEN_RESPONDER_REPLY_CONCURRENCY", "3"))),
        )
        try:
            self.tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        except Exception:
            self.tz = timezone.utc
        state_value = str(state_file or "").strip()

        if (
            not state_value
            and self.tenant_user_id is not None
        ):
            state_value = (
                f"/app/data/tenant_dzen/"
                f"u{self.tenant_user_id}/state.json"
            )

        self.state_path = Path(
            state_value
            or os.getenv(
                "DZEN_RESPONDER_STATE_FILE",
                "data/dzen_comment_responder_state.json",
            )
        )

        debug_value = str(debug_dir or "").strip()

        if (
            not debug_value
            and self.tenant_user_id is not None
        ):
            debug_value = (
                f"/app/data/tenant_dzen/"
                f"u{self.tenant_user_id}/debug"
            )

        self.debug_dir = Path(
            debug_value
            or os.getenv(
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
        state.setdefault("reply_prompt", DEFAULT_REPLY_PROMPT)
        self._save_state(state)

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._runtime_progress: dict[str, Any] = {
            "active": False,
            "scope": "",
            "stage": "idle",
            "done": 0,
            "total": 0,
            "detail": "",
        }

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
            "week_days": self.week_days,
            "week_scroll_rounds": self.week_scroll_rounds,
            "reply_timeout_seconds": self.reply_timeout_seconds,
            "reply_concurrency": self.reply_concurrency,
            "prompt_custom": self.get_reply_prompt().strip() != DEFAULT_REPLY_PROMPT.strip(),
            "runtime": dict(self._runtime_progress),
            "processed_count": len(processed),
            "replied": int(stats.get("replied", 0) or 0),
            "skipped": int(stats.get("skipped", 0) or 0),
            "errors": int(stats.get("errors", 0) or 0),
            "dry_runs": int(stats.get("dry_runs", 0) or 0),
            "last_cycle_at": state.get("last_cycle_at"),
            "last_reply_at": state.get("last_reply_at"),
        }

    def runtime_status(self) -> dict[str, Any]:
        return dict(self._runtime_progress)

    def _set_runtime(
        self,
        *,
        active: bool | None = None,
        scope: str | None = None,
        stage: str | None = None,
        done: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        if active is not None:
            self._runtime_progress["active"] = bool(active)
        if scope is not None:
            self._runtime_progress["scope"] = str(scope)
        if stage is not None:
            self._runtime_progress["stage"] = str(stage)
        if done is not None:
            self._runtime_progress["done"] = int(done)
        if total is not None:
            self._runtime_progress["total"] = int(total)
        if detail is not None:
            self._runtime_progress["detail"] = str(detail)

    def _clear_runtime(self) -> None:
        self._runtime_progress.update({
            "active": False,
            "scope": "",
            "stage": "idle",
            "done": 0,
            "total": 0,
            "detail": "",
        })

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

    def get_reply_prompt(self) -> str:
        state = self._load_state()
        prompt = str(state.get("reply_prompt") or "").strip()
        return prompt or DEFAULT_REPLY_PROMPT

    def set_reply_prompt(self, prompt: str) -> str:
        clean = str(prompt or "").strip()
        if not clean:
            raise ValueError("Промпт не может быть пустым")
        state = self._load_state()
        state["reply_prompt"] = clean
        state["reply_prompt_updated_at"] = self._now()
        self._save_state(state)
        return clean

    def reset_reply_prompt(self) -> str:
        state = self._load_state()
        state["reply_prompt"] = DEFAULT_REPLY_PROMPT
        state["reply_prompt_updated_at"] = self._now()
        self._save_state(state)
        return DEFAULT_REPLY_PROMPT

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
        if force and self._run_lock.locked():
            return {"status": "busy", "runtime": self.runtime_status()}

        async with self._run_lock:
            self._set_runtime(active=True, scope="cycle", stage="scan", done=0, total=0, detail="Сканирую комментарии Дзена")
            try:
                log.info("Dzen responder: начинаю обычный скан комментариев")
                comments = await self._scan_comments()
                log.info("Dzen responder: скан завершён, найдено карточек=%s", len(comments))
                return await self._process_comments(
                    comments,
                    limit=self.max_per_cycle,
                    scope="cycle",
                )
            finally:
                self._clear_runtime()

    async def run_week(self, *, force: bool = False) -> dict[str, Any]:
        """Ответить на все ещё не обработанные комментарии за последние N дней."""
        if not self.enabled and not force:
            return {"status": "disabled"}
        if not self.comments_url:
            return {
                "status": "config_error",
                "error": "Не задан DZEN_COMMENTS_URL/DZEN_RESPONDER_COMMENTS_URL",
            }
        if force and self._run_lock.locked():
            return {"status": "busy", "runtime": self.runtime_status()}

        async with self._run_lock:
            self._set_runtime(active=True, scope="week", stage="scan", done=0, total=0, detail="Сканирую комментарии за 7 дней")
            try:
                log.info("Dzen responder: начинаю недельный скан, scroll_rounds=%s", self.week_scroll_rounds)
                comments = await self._scan_comments(scroll_rounds=self.week_scroll_rounds)
                recent: list[ResponderComment] = []
                older = 0
                undated = 0
                for item in comments:
                    recent_flag = self._is_recent_comment(item.created_raw, days=self.week_days)
                    if recent_flag is True:
                        recent.append(item)
                    elif recent_flag is False:
                        older += 1
                    else:
                        undated += 1

                log.info(
                    "Dzen responder: недельный скан завершён total=%s recent=%s older=%s undated=%s",
                    len(comments), len(recent), older, undated,
                )
                result = await self._process_comments(
                    recent,
                    limit=None,
                    scope="week",
                )
                result.update(
                    {
                        "week_days": self.week_days,
                        "scanned_total": len(comments),
                        "weekly_candidates": len(recent),
                        "older": older,
                        "undated": undated,
                    }
                )
                return result
            finally:
                self._clear_runtime()

    async def _process_comments(
        self,
        comments: list[ResponderComment],
        *,
        limit: int | None,
        scope: str,
    ) -> dict[str, Any]:
        state = self._load_state()
        processed: dict[str, Any] = state.setdefault(
            "processed",
            {},
        )

        daily_remaining: int | None = None

        if (
            self.tenant_user_id is not None
            and self.daily_reply_limit is not None
        ):
            daily_used = (
                await tenant_db.successful_dzen_replies_today(
                    self.tenant_user_id,
                    getattr(
                        self.cfg,
                        "timezone",
                        "Europe/Moscow",
                    ),
                )
            )

            daily_remaining = max(
                0,
                self.daily_reply_limit - daily_used,
            )

            log.info(
                "Tenant Dzen daily replies: "
                "user=%s used=%s limit=%s remaining=%s",
                self.tenant_user_id,
                daily_used,
                self.daily_reply_limit,
                daily_remaining,
            )

            if daily_remaining <= 0:
                state["last_cycle_at"] = self._now()
                self._save_state(state)

                return {
                    "status": "daily_reply_limit",
                    "found": len(comments),
                    "new": 0,
                    "replied": 0,
                    "daily_used": daily_used,
                    "daily_limit": self.daily_reply_limit,
                    "scope": scope,
                }

        fresh = [
            item
            for item in comments
            if item.key not in processed
        ]
        if not fresh:
            state["last_cycle_at"] = self._now()
            self._save_state(state)
            return {
                "status": "no_new_comments",
                "found": len(comments),
                "new": 0,
                "scope": scope,
            }

        effective_limit = limit

        if daily_remaining is not None:
            if effective_limit is None:
                effective_limit = daily_remaining
            else:
                effective_limit = min(
                    effective_limit,
                    daily_remaining,
                )

        selected = (
            fresh
            if effective_limit is None
            else fresh[:effective_limit]
        )

        total = len(selected)
        self._set_runtime(stage="generating", done=0, total=total, detail="Генерирую ответы через YandexGPT")
        log.info(
            "Dzen responder: генерация ответов start scope=%s selected=%s concurrency=%s timeout=%ss",
            scope, total, self.reply_concurrency, self.reply_timeout_seconds,
        )

        semaphore = asyncio.Semaphore(self.reply_concurrency)

        async def generate_one(index: int, item: ResponderComment) -> tuple[int, ResponderComment, str, str, str]:
            if self._is_own_comment(item):
                return index, item, "", "skipped_own", ""
            try:
                async with semaphore:
                    reply = await self._generate_reply(item)
                if not reply:
                    return index, item, "", "skipped_ai", ""
                return index, item, reply, "ok", ""
            except ContentBlockedError as exc:
                log.warning(
                    "Dzen responder: safety skip comment=%s error=%s",
                    item.comment_id or item.key,
                    exc,
                )
                return (
                    index,
                    item,
                    "",
                    "skipped_safety",
                    str(exc),
                )
            except Exception as exc:
                log.exception(
                    "Dzen responder: ошибка YandexGPT для %s",
                    item.key,
                )
                return index, item, "", "error", str(exc)
            finally:
                # Счётчик прогресса безопасен в рамках одного event loop.
                self._set_runtime(done=min(total, int(self._runtime_progress.get("done", 0)) + 1))
                log.info(
                    "Dzen responder: генерация progress %s/%s",
                    self._runtime_progress.get("done", 0), total,
                )

        generated = await asyncio.gather(
            *(generate_one(i, item) for i, item in enumerate(selected)),
            return_exceptions=False,
        )
        generated.sort(key=lambda row: row[0])

        plans: list[tuple[ResponderComment, str]] = []
        skipped = 0
        ai_errors = 0
        for _, item, reply, status, error in generated:
            if status == "ok":
                plans.append((item, reply))
                continue
            if status == "skipped_own":
                self._mark_processed(state, item, status="skipped_own", reply="")
                skipped += 1
                continue
            if status == "skipped_ai":
                self._mark_processed(
                    state,
                    item,
                    status="skipped_ai",
                    reply="",
                )
                skipped += 1
                self._bump_stat(state, "skipped")
                continue

            if status == "skipped_safety":
                self._mark_processed(
                    state,
                    item,
                    status="skipped_safety",
                    reply="",
                )
                skipped += 1
                self._bump_stat(state, "skipped")

                log.info(
                    "Dzen responder: comment permanently skipped "
                    "by safety filter comment=%s",
                    item.comment_id or item.key,
                )
                continue

            ai_errors += 1
            self._bump_stat(state, "errors")
            log.error("Dzen responder: YandexGPT failed comment=%s error=%s", item.comment_id or item.key, error)

        log.info(
            "Dzen responder: генерация завершена prepared=%s skipped=%s errors=%s",
            len(plans), skipped, ai_errors,
        )

        if self.dry_run:
            previews = []
            for item, reply in plans:
                previews.append(
                    {
                        "comment_id": item.comment_id,
                        "author": item.author,
                        "comment": item.text[:300],
                        "reply": reply,
                        "created_raw": item.created_raw,
                    }
                )
            self._bump_stat(state, "dry_runs", len(plans))
            state["last_cycle_at"] = self._now()
            self._save_state(state)
            return {
                "status": "dry_run",
                "found": len(comments),
                "new": len(fresh),
                "selected": len(selected),
                "prepared": len(plans),
                "skipped": skipped,
                "ai_errors": ai_errors,
                "previews": previews,
                "scope": scope,
            }

        posted = 0
        post_errors = 0

        if plans:
            self._set_runtime(
                stage="posting",
                done=0,
                total=len(plans),
                detail="Публикую ответы в Дзене",
            )

            results = await self._post_replies(
                plans,
                state=state,
            )

            for item, reply, ok, error, verified in results:
                if ok:
                    posted += 1
                else:
                    post_errors += 1

        state["last_cycle_at"] = self._now()
        self._save_state(state)
        return {
            "status": "ok",
            "found": len(comments),
            "new": len(fresh),
            "selected": len(selected),
            "replied": posted,
            "skipped": skipped,
            "errors": ai_errors + post_errors,
            "scope": scope,
        }

    async def _extract_comments_from_page(self, page: Page) -> list[dict[str, Any]]:
        """Минимальный быстрый парсер текущего DOM Дзена."""
        return await page.evaluate(
            """
            () => {
              const cards = [
                ...document.querySelectorAll(
                  'div[class*="editor--comment__block"]'
                )
              ];

              const result = [];

              for (const card of cards) {
                const lines = String(
                  card.innerText || card.textContent || ''
                )
                  .replace(/\\u00a0/g, ' ')
                  .split(/\\n+/)
                  .map(x => x.trim())
                  .filter(Boolean);

                if (lines.length < 4) {
                  continue;
                }

                const subscriberIndex = lines.findIndex(
                  x => /^(подписчик|автор)$/i.test(x)
                );

                if (subscriberIndex <= 0) {
                  continue;
                }

                const author =
                  lines[subscriberIndex - 1] || '';

                const createdRaw =
                  lines[subscriberIndex + 1] || '';

                const textStart =
                  subscriberIndex + 2;

                let textEnd = lines.length;

                for (
                  let i = textStart;
                  i < lines.length;
                  i++
                ) {
                  if (/^ответить$/i.test(lines[i])) {
                    textEnd = i;
                    break;
                  }
                }

                const text = lines
                  .slice(textStart, textEnd)
                  .join('\\n')
                  .trim();

                if (!text) {
                  continue;
                }

                result.push({
                  href: location.href,
                  text,
                  author,
                  articleTitle: '',
                  createdRaw,
                  commentId: ''
                });
              }

              return result;
            }
            """
        )

    async def _scan_comments(
        self,
        *,
        scroll_rounds: int | None = None,
    ) -> list[ResponderComment]:
        profile = Path(self.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)

        raw_by_key: dict[str, dict[str, Any]] = {}

        log.debug("Dzen responder DEBUG: жду DZEN_BROWSER_LOCK")
        async with DZEN_BROWSER_LOCK:
            log.debug("Dzen responder DEBUG: DZEN_BROWSER_LOCK получен")
            async with async_playwright() as pw:
                log.debug("Dzen responder DEBUG: Playwright запущен")
                log.debug("Dzen responder DEBUG: запускаю Chromium persistent context")
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 1000},
                    locale="ru-RU",
                )
                try:
                    log.debug("Dzen responder DEBUG: Chromium запущен")
                    page = context.pages[0] if context.pages else await context.new_page()
                    log.debug("Dzen responder DEBUG: начинаю page.goto")
                    await page.goto(
                        self.comments_url,
                        wait_until="domcontentloaded",
                        timeout=90000,
                    )
                    log.debug(
                        "Dzen responder DEBUG: page.goto завершён url=%s",
                        page.url,
                    )
                    self._assert_authorized(page)
                    log.debug("Dzen responder DEBUG: авторизация проверена")

                    # Студия догружает комментарии после DOMContentLoaded. Ждём карточки,
                    # но отсутствие selector не считаем исключением — ниже есть fallback/debug.
                    try:
                        log.debug("Dzen responder DEBUG: жду reply-button")
                        await page.wait_for_selector(
                            'button[data-testid="reply-button"], '
                            'a[aria-label="Комментарий"], a[href*="#comment_"]',
                            timeout=15000,
                        )
                        log.debug("Dzen responder DEBUG: reply-button найден")
                    except Exception as exc:
                        log.warning(
                            "Dzen responder: ошибка ожидания reply-button: %s",
                            exc,
                        )
                        await page.wait_for_timeout(2500)

                    rounds = max(
                        1,
                        int(scroll_rounds or self.scroll_rounds),
                    )
                    no_growth_rounds = 0

                    for round_index in range(rounds + 1):
                        log.info(
                            "Dzen responder: extract start round=%s/%s",
                            round_index + 1,
                            rounds + 1,
                        )

                        batch = await asyncio.wait_for(
                            self._extract_comments_from_page(page),
                            timeout=10,
                        )

                        log.info(
                            "Dzen responder: extract done round=%s visible=%s",
                            round_index + 1,
                            len(batch),
                        )

                        before_count = len(raw_by_key)

                        for item in batch:
                            text_value = " ".join(
                                str(item.get("text") or "").split()
                            ).strip()

                            if not text_value:
                                continue

                            identity = "\n".join([
                                str(item.get("commentId") or ""),
                                str(item.get("author") or ""),
                                str(item.get("createdRaw") or ""),
                                text_value,
                            ])

                            raw_key = hashlib.sha256(
                                identity.encode("utf-8")
                            ).hexdigest()

                            raw_by_key[raw_key] = item

                        after_count = len(raw_by_key)

                        log.info(
                            "Dzen responder: scan progress "
                            "round=%s/%s visible=%s unique=%s new=%s",
                            round_index + 1,
                            rounds + 1,
                            len(batch),
                            after_count,
                            after_count - before_count,
                        )

                        if after_count == before_count:
                            no_growth_rounds += 1
                        else:
                            no_growth_rounds = 0

                        if raw_by_key and no_growth_rounds >= 3:
                            log.info(
                                "Dzen responder: список стабилен, "
                                "останавливаю скан unique=%s",
                                len(raw_by_key),
                            )
                            break

                        if round_index >= rounds:
                            break

                        # Скроллим только страницу и контейнеры
                        # интерфейса комментариев, а не тысячи DIV.
                        await page.evaluate(
                            """
                            () => {
                              const root =
                                document.scrollingElement ||
                                document.documentElement;

                              if (root) {
                                root.scrollTop = Math.min(
                                  root.scrollTop + 3000,
                                  root.scrollHeight
                                );
                              }

                              const candidates =
                                document.querySelectorAll(
                                  '[class*="comments-page__"]'
                                );

                              for (const el of candidates) {
                                try {
                                  if (
                                    el.scrollHeight >
                                    el.clientHeight + 150
                                  ) {
                                    el.scrollTop = Math.min(
                                      el.scrollTop + 3000,
                                      el.scrollHeight
                                    );
                                  }
                                } catch (_) {}
                              }
                            }
                            """
                        )

                        try:
                            await page.mouse.wheel(0, 2500)
                        except Exception:
                            pass

                        await page.wait_for_timeout(350)

                    if not raw_by_key:
                        log.warning(
                            "Dzen responder: скан вернул 0 карточек, url=%s title=%s",
                            page.url,
                            await page.title(),
                        )
                        await self._save_debug(page, "scan_zero")
                except Exception:
                    await self._save_debug(page, "scan_error")
                    raise
                finally:
                    await context.close()

        raw = list(raw_by_key.values())
        result: list[ResponderComment] = []
        seen: set[str] = set()
        for item in raw:
            href = str(item.get("href") or "").strip()
            text = " ".join(str(item.get("text") or "").split()).strip()
            author = " ".join(str(item.get("author") or "").split()).strip()
            article_title = " ".join(str(item.get("articleTitle") or "").split()).strip()
            created_raw = " ".join(str(item.get("createdRaw") or "").split()).strip()
            if not text or len(text) < 2:
                continue
            href = urljoin(
                self.comments_url,
                href or self.comments_url,
            )

            # n_reply = явный признак «без ответа». Значение all/отсутствие параметра
            # не означает, что конкретный комментарий уже обработан: это может быть
            # просто режим списка Студии. Поэтому больше не выбрасываем такие карточки.
            # Защита от наших повторов остаётся через persistent state processed[].
            low_href = href.lower()
            if re.search(r"comments_data=(?:reply|replied|answered|with_reply|y_reply)(?:[&#]|$)", low_href):
                continue

            explicit_comment_id = str(
                item.get("commentId") or ""
            ).strip()

            m = re.search(
                r"#comment_([A-Za-z0-9_-]+)",
                href,
            )

            comment_id = (
                explicit_comment_id
                or (m.group(1) if m else "")
            )
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
                    created_raw=created_raw[:120],
                )
            )
        return result


    def _is_recent_comment(self, raw: str, *, days: int) -> bool | None:
        parsed = self._parse_comment_datetime(raw)
        if parsed is None:
            return None
        now = datetime.now(self.tz)
        # Для подписей без времени (например, «11 августа») работаем по календарным дням.
        delta_days = (now.date() - parsed.astimezone(self.tz).date()).days
        return 0 <= delta_days < days

    def _parse_comment_datetime(self, raw: str) -> datetime | None:
        text = " ".join(str(raw or "").strip().lower().replace("ё", "е").split())
        if not text:
            return None
        now = datetime.now(self.tz)

        # ISO datetime, если Дзен отдаёт <time datetime=...>.
        try:
            iso = str(raw).strip().replace("Z", "+00:00")
            value = datetime.fromisoformat(iso)
            if value.tzinfo is None:
                value = value.replace(tzinfo=self.tz)
            return value.astimezone(self.tz)
        except Exception:
            pass

        if text.startswith("сегодня"):
            return now
        if text.startswith("вчера"):
            return now - timedelta(days=1)

        m = re.search(r"(\d+)\s*(?:минут|минута|минуты|мин)(?:\s*назад)?\b", text)
        if m:
            return now - timedelta(minutes=int(m.group(1)))
        m = re.search(r"(\d+)\s*(?:час|часа|часов|ч)(?:\s*назад)?\b", text)
        if m:
            return now - timedelta(hours=int(m.group(1)))
        m = re.search(r"(\d+)\s*(?:день|дня|дней|дн|д)(?:\s*назад)?\b", text)
        if m:
            return now - timedelta(days=int(m.group(1)))
        m = re.search(r"(\d+)\s*(?:неделя|недели|недель|нед)(?:\s*назад)?\b", text)
        if m:
            return now - timedelta(weeks=int(m.group(1)))

        months = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
            "мая": 5, "июня": 6, "июля": 7, "августа": 8,
            "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
        }
        m = re.search(
            r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?\b",
            text,
        )
        if m:
            day = int(m.group(1))
            month = months[m.group(2)]
            year = int(m.group(3)) if m.group(3) else now.year
            try:
                value = datetime(year, month, day, 12, 0, tzinfo=self.tz)
                # В январе подпись «31 декабря» относится к предыдущему году.
                if not m.group(3) and value > now + timedelta(days=2):
                    value = value.replace(year=year - 1)
                return value
            except ValueError:
                return None

        m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text)
        if m:
            day = int(m.group(1))
            month = int(m.group(2))
            year_raw = m.group(3)
            year = now.year if not year_raw else int(year_raw)
            if year < 100:
                year += 2000
            try:
                value = datetime(year, month, day, 12, 0, tzinfo=self.tz)
                if not year_raw and value > now + timedelta(days=2):
                    value = value.replace(year=year - 1)
                return value
            except ValueError:
                return None

        return None

    async def _generate_reply(
        self,
        item: ResponderComment,
    ) -> str:
        prompt = (
            f"ЗАГОЛОВОК МАТЕРИАЛА:\n"
            f"{item.article_title or 'не указан'}\n\n"
            f"АВТОР КОММЕНТАРИЯ:\n"
            f"{item.author or 'не указан'}\n\n"
            f"КОММЕНТАРИЙ:\n"
            f"{item.text}"
        )

        auth = await self.gpt.auth_header()

        # Используем текущий промпт пользователя
        # БЕЗ скрытых дополнений программы.
        reply_prompt = self.get_reply_prompt()

        with usage_context(
            "comment_reply",
            user_id=getattr(self, "user_id", None),
            metadata={
                "article_title": item.article_title,
            },
        ):
            raw = await asyncio.to_thread(
                self.gpt._complete_sync,
                auth,
                reply_prompt,
                prompt,
                self.reply_timeout_seconds,
            )

        reply = self._clean_reply(
            str(raw or "")
        )

        if not reply:
            return ""

        # Только ограничиваем длину.
        # Никакие ссылки программа сама
        # больше не добавляет.
        return self._truncate_reply(
            reply
        )


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
        *,
        state: dict[str, Any],
    ) -> list[tuple[ResponderComment, str, bool, str, bool]]:
        profile = Path(self.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)

        results: list[
            tuple[ResponderComment, str, bool, str, bool]
        ] = []

        total = len(plans)

        log.info(
            "Dzen responder: публикация start total=%s",
            total,
        )

        async with DZEN_BROWSER_LOCK:
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 1000},
                    locale="ru-RU",
                )

                try:
                    page = (
                        context.pages[0]
                        if context.pages
                        else await context.new_page()
                    )

                    for idx, (item, reply) in enumerate(plans, 1):
                        comment_ref = (
                            item.comment_id
                            or item.key
                        )

                        self._set_runtime(
                            stage="posting",
                            done=idx - 1,
                            total=total,
                            detail=(
                                f"Публикую ответы в Дзене… "
                                f"{idx - 1}/{total}"
                            ),
                        )

                        log.info(
                            "Dzen responder: публикация "
                            "start %s/%s comment=%s author=%s",
                            idx,
                            total,
                            comment_ref,
                            item.author,
                        )

                        try:
                            verified = await asyncio.wait_for(
                                self._post_one(
                                    page,
                                    item,
                                    reply,
                                ),
                                timeout=60,
                            )

                            # Дневной лимит tenant списываем
                            # только после реально подтверждённого
                            # ответа в DOM Дзена.
                            if (
                                self.tenant_user_id is not None
                                and verified
                            ):
                                recorded = (
                                    await tenant_db.record_tenant_dzen_reply(
                                        self.tenant_user_id,
                                        comment_ref,
                                    )
                                )

                                log.info(
                                    "Tenant Dzen reply recorded: "
                                    "user=%s comment=%s recorded=%s",
                                    self.tenant_user_id,
                                    comment_ref,
                                    recorded,
                                )

                            results.append(
                                (
                                    item,
                                    reply,
                                    True,
                                    "",
                                    verified,
                                )
                            )

                            self._mark_processed(
                                state,
                                item,
                                status=(
                                    "replied"
                                    if verified
                                    else "replied_unverified"
                                ),
                                reply=reply,
                            )

                            self._bump_stat(
                                state,
                                "replied",
                            )

                            state["last_reply_at"] = self._now()

                            # КРИТИЧНО:
                            # сохраняем сразу после каждого ответа.
                            # При аварийном рестарте этот комментарий
                            # уже не попадёт в очередь повторно.
                            self._save_state(state)

                            self._set_runtime(
                                stage="posting",
                                done=idx,
                                total=total,
                                detail=(
                                    f"Публикую ответы в Дзене… "
                                    f"{idx}/{total}"
                                ),
                            )

                            log.info(
                                "Dzen responder: публикация "
                                "done %s/%s comment=%s "
                                "verified=%s",
                                idx,
                                total,
                                comment_ref,
                                verified,
                            )

                        except asyncio.TimeoutError:
                            error = (
                                "Таймаут публикации ответа "
                                "после 60 секунд"
                            )

                            self._bump_stat(
                                state,
                                "errors",
                            )
                            self._save_state(state)

                            log.error(
                                "Dzen responder: публикация "
                                "timeout %s/%s comment=%s",
                                idx,
                                total,
                                comment_ref,
                            )

                            try:
                                await self._save_debug(
                                    page,
                                    f"reply_timeout_"
                                    f"{item.comment_id or 'unknown'}",
                                )
                            except Exception:
                                pass

                            results.append(
                                (
                                    item,
                                    reply,
                                    False,
                                    error,
                                    False,
                                )
                            )

                            # Ошибка одного комментария
                            # не должна останавливать очередь.
                            continue

                        except Exception as exc:
                            error = str(exc)

                            self._bump_stat(
                                state,
                                "errors",
                            )
                            self._save_state(state)

                            log.error(
                                "Dzen responder: публикация "
                                "error %s/%s comment=%s: %s",
                                idx,
                                total,
                                comment_ref,
                                error,
                            )

                            try:
                                await self._save_debug(
                                    page,
                                    f"reply_error_"
                                    f"{item.comment_id or 'unknown'}",
                                )
                            except Exception:
                                pass

                            results.append(
                                (
                                    item,
                                    reply,
                                    False,
                                    error,
                                    False,
                                )
                            )

                            # Переходим к следующему,
                            # а не останавливаем всю пачку.
                            continue

                finally:
                    await context.close()

        log.info(
            "Dzen responder: публикация завершена "
            "total=%s success=%s errors=%s",
            total,
            sum(1 for r in results if r[2]),
            sum(1 for r in results if not r[2]),
        )

        return results

    async def _post_one(
        self,
        page: Page,
        item: ResponderComment,
        reply: str,
    ) -> bool:
        await page.goto(
            item.href,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        await page.wait_for_timeout(2500)
        self._assert_authorized(page)

        # Ищем строго карточку нужного комментария.
        target = await self._find_comment_element(
            page,
            item,
        )
        if target is None:
            raise RuntimeError(
                "Не найдена карточка нужного комментария"
            )

        try:
            await target.scroll_into_view_if_needed()
        except Exception:
            pass

        # Только локальная кнопка внутри найденной карточки.
        reply_button = await self._find_reply_button(
            page,
            target,
        )
        if reply_button is None:
            raise RuntimeError(
                "Не найдена локальная кнопка «Ответить»"
            )

        await reply_button.click(
            timeout=7000,
        )
        await page.wait_for_timeout(500)

        composer = await self._find_composer(
            page,
            target,
        )
        if composer is None:
            raise RuntimeError(
                "После «Ответить» не найдено поле Ваш ответ..."
            )

        await self._fill_composer(
            page,
            composer,
            reply,
        )

        await page.wait_for_timeout(300)

        # В текущем DOM Дзена настоящая кнопка отправки:
        # button[data-testid="send-button"]
        send_button = await self._find_send_button(
            page,
            composer,
        )
        if send_button is None:
            raise RuntimeError(
                "Не найдена кнопка data-testid=send-button"
            )

        if not await send_button.is_enabled():
            raise RuntimeError(
                "Кнопка отправки ответа неактивна"
            )

        log.info(
            "Dzen responder: нажимаю send-button "
            "comment=%s author=%s",
            item.comment_id or item.key,
            item.author,
        )

        await send_button.click(
            timeout=7000,
        )

        # Теперь НЕ считаем сам клик успешной публикацией.
        # Обязательно ждём появления нашего текста
        # среди реальных p «Текст комментария».
        needle = " ".join(reply.split()).strip()

        if len(needle) < 12:
            raise RuntimeError(
                "Ответ слишком короткий для проверки публикации"
            )

        probe = needle[:70].lower()

        # Родительская ветка комментария.
        thread = target.locator(
            "xpath=ancestor::div"
            "[contains(@class,'comments-page__commentNode')][1]"
        )

        show_replies_clicked = False

        for attempt in range(24):
            try:
                texts = thread.locator(
                    'p[aria-label="Текст комментария"]'
                )

                count = await texts.count()

                for index in range(count):
                    value = " ".join(
                        (
                            await texts.nth(index).inner_text()
                        ).split()
                    ).strip().lower()

                    if probe in value:
                        log.info(
                            "Dzen responder: ответ подтверждён "
                            "в DOM comment=%s",
                            item.comment_id or item.key,
                        )
                        return True

            except Exception:
                pass

            # Если Дзен свернул ответы — раскрываем ветку
            # и проверяем повторно.
            if attempt >= 3 and not show_replies_clicked:
                try:
                    buttons = thread.get_by_role(
                        "button",
                        name=re.compile(
                            r"^Показать\s+\d+\s+ответ",
                            re.I,
                        ),
                    )

                    for i in range(await buttons.count()):
                        btn = buttons.nth(i)
                        if await btn.is_visible():
                            await btn.click(timeout=5000)
                            show_replies_clicked = True
                            await page.wait_for_timeout(500)
                            break
                except Exception:
                    pass

            await page.wait_for_timeout(500)

        # Критично: если текста нет в DOM,
        # НЕ помечаем комментарий как replied.
        raise RuntimeError(
            "После нажатия send-button ответ "
            "не появился в ветке комментария"
        )

    async def _find_comment_element(
        self,
        page: Page,
        item: ResponderComment,
    ) -> Locator | None:

        def normalize(value: str) -> str:
            return " ".join(
                str(value or "")
                .replace("\u00a0", " ")
                .replace("\u200b", "")
                .replace("\u2060", "")
                .replace("\ufeff", "")
                .split()
            ).strip()

        target_text = normalize(item.text)
        target_author = normalize(item.author).casefold()

        async def search_loaded() -> Locator | None:
            cards = page.locator(
                'div[class*="editor--comment__block"]'
            )

            count = await cards.count()

            for index in range(count):
                card = cards.nth(index)

                try:
                    if not await card.is_visible():
                        continue

                    text_element = card.locator(
                        'p[aria-label="Текст комментария"]'
                    ).first

                    if not await text_element.count():
                        continue

                    actual_text = normalize(
                        await text_element.inner_text()
                    )

                    # Сравниваем именно текст комментария.
                    if actual_text != target_text:
                        continue

                    # Определяем автора этой конкретной карточки.
                    raw = await card.inner_text()

                    lines = [
                        normalize(x)
                        for x in raw.splitlines()
                        if normalize(x)
                    ]

                    subscriber_index = -1

                    for i, line in enumerate(lines):
                        if line.casefold() in (
                            "подписчик",
                            "автор",
                        ):
                            subscriber_index = i
                            break

                    actual_author = ""

                    if subscriber_index > 0:
                        actual_author = lines[
                            subscriber_index - 1
                        ].casefold()

                    if (
                        target_author
                        and actual_author
                        and actual_author != target_author
                    ):
                        continue

                    log.info(
                        "Dzen responder: точная карточка найдена "
                        "author=%s text=%s",
                        item.author,
                        target_text[:90],
                    )

                    return card

                except Exception:
                    continue

            return None

        # Сначала ищем среди уже загруженных карточек.
        found = await search_loaded()

        if found is not None:
            return found

        empty_scrolls = 0

        # Затем последовательно раскрываем ветки
        # «Показать N ответов» и прокручиваем страницу.
        for attempt in range(120):

            found = await search_loaded()

            if found is not None:
                return found

            buttons = page.locator("button")
            button_count = await buttons.count()

            clicked = False

            for index in range(button_count):
                try:
                    button = buttons.nth(index)

                    if not await button.is_visible():
                        continue

                    label = normalize(
                        await button.inner_text()
                    )

                    low = label.casefold()

                    if not low.startswith("показать"):
                        continue

                    if "ответ" not in low:
                        continue

                    await button.click(
                        timeout=3500,
                    )

                    log.debug(
                        "Dzen responder: раскрываю ветку %s",
                        label,
                    )

                    clicked = True
                    empty_scrolls = 0

                    await page.wait_for_timeout(180)

                    found = await search_loaded()

                    if found is not None:
                        log.info(
                            "Dzen responder: карточка найдена "
                            "после раскрытия ветки author=%s",
                            item.author,
                        )
                        return found

                    # DOM после клика изменился,
                    # поэтому перечитываем список кнопок.
                    break

                except Exception:
                    continue

            if clicked:
                continue

            # На текущем экране раскрывать больше нечего.
            # Прокручиваем вниз, чтобы появились следующие ветки.
            empty_scrolls += 1

            try:
                await page.evaluate(
                    """
                    () => {
                      const root =
                        document.scrollingElement ||
                        document.documentElement;

                      if (root) {
                        root.scrollTop = Math.min(
                          root.scrollTop + 2400,
                          root.scrollHeight
                        );
                      }

                      const containers =
                        document.querySelectorAll(
                          '[class*="comments-page__"]'
                        );

                      for (const el of containers) {
                        try {
                          if (
                            el.scrollHeight >
                            el.clientHeight + 100
                          ) {
                            el.scrollTop = Math.min(
                              el.scrollTop + 2400,
                              el.scrollHeight
                            );
                          }
                        } catch (_) {}
                      }
                    }
                    """
                )

                await page.mouse.wheel(
                    0,
                    2200,
                )

            except Exception:
                pass

            await page.wait_for_timeout(250)

            found = await search_loaded()

            if found is not None:
                log.info(
                    "Dzen responder: карточка найдена "
                    "после прокрутки author=%s",
                    item.author,
                )
                return found

            # Диагностика показала, что 12 прокруток
            # достаточно для загрузки глубоких карточек.
            if empty_scrolls >= 20:
                break

        log.warning(
            "Dzen responder: точная карточка окончательно "
            "не найдена author=%s text=%s",
            item.author,
            target_text[:120],
        )

        return None

    async def _find_reply_button(
        self,
        page: Page,
        target: Locator | None,
    ) -> Locator | None:
        if target is None:
            return None

        candidate = target.locator(
            'button[data-testid="reply-button"]'
        ).first

        try:
            if (
                await candidate.count()
                and await candidate.is_visible()
            ):
                return candidate
        except Exception:
            pass

        return None

    async def _find_composer(
        self,
        page: Page,
        target: Locator | None,
    ) -> Locator | None:
        if target is None:
            return None

        candidate = target.locator(
            'textarea[data-testid="comment-textarea"]'
        ).last

        try:
            if (
                await candidate.count()
                and await candidate.is_visible()
            ):
                return candidate
        except Exception:
            pass

        return None

    async def _fill_composer(
        self,
        page: Page,
        composer: Locator,
        reply: str,
    ) -> None:
        # Дзен сам подставляет в textarea:
        # "Имя пользователя, "
        # Сохраняем этот префикс, чтобы ответ оставался
        # именно ответом адресату.
        prefix = ""

        try:
            prefix = await composer.input_value()
        except Exception:
            prefix = ""

        prefix = str(prefix or "")

        if prefix:
            prefix_clean = " ".join(
                prefix.split()
            ).strip().lower()

            reply_clean = reply.lstrip().lower()

            if (
                prefix_clean
                and reply_clean.startswith(prefix_clean)
            ):
                value = reply
            else:
                value = prefix + reply
        else:
            value = reply

        await composer.fill(value)

    async def _find_send_button(
        self,
        page: Page,
        composer: Locator,
    ) -> Locator | None:
        container = composer.locator(
            'xpath=ancestor::*'
            '[@data-testid="comment-form-container"][1]'
        )

        try:
            if not await container.count():
                return None

            candidate = container.locator(
                'button[data-testid="send-button"]'
            ).first

            if (
                await candidate.count()
                and await candidate.is_visible()
            ):
                return candidate

        except Exception:
            pass

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
            "created_raw": item.created_raw,
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
