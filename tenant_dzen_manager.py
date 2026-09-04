from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import tenant_db
from dzen_comment_responder import DzenCommentResponderWorker

from dzen_popular_comments import (
    DzenPopularCommentWorker,
)


log = logging.getLogger("tenant_dzen_manager")


class _TenantPopularArticleAdapter:
    """
    Связывает popular-comment worker
    с TenantArticleService конкретного пользователя.
    """

    def __init__(
        self,
        service: Any,
        user_id: int,
    ) -> None:
        self.service = service
        self.user_id = int(user_id)

    async def publish_manual_topic(
        self,
        topic_title: str,
    ) -> dict[str, Any]:
        return await self.service.publish_manual_topic(
            self.user_id,
            topic_title,
            trigger="popular_comment",
        )


class TenantDzenManager:
    def __init__(
        self,
        *,
        gpt_client: Any,
        cfg: Any,
        article_service: Any | None = None,
    ) -> None:
        self.gpt_client = gpt_client
        self.cfg = cfg

        self.article_service = article_service
        self.workers: dict[
            int,
            DzenCommentResponderWorker,
        ] = {}

        self.tasks: dict[
            int,
            asyncio.Task,
        ] = {}

        self.popular_workers: dict[
            int,
            DzenPopularCommentWorker,
        ] = {}

        self.popular_tasks: dict[
            int,
            asyncio.Task,
        ] = {}

        self.fingerprints: dict[
            int,
            tuple[str, str, str],
        ] = {}

        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

        for worker in self.popular_workers.values():
            worker.stop()

        for worker in self.workers.values():
            worker.stop()

    async def run(self) -> None:
        log.info(
            "Tenant Dzen manager запущен"
        )

        try:
            while not self._stop.is_set():
                try:
                    await self.sync()
                except Exception:
                    log.exception(
                        "Tenant Dzen manager: "
                        "ошибка синхронизации"
                    )

                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            for user_id in list(
                self.workers.keys()
            ):
                await self._stop_worker(user_id)

            log.info(
                "Tenant Dzen manager остановлен"
            )

    async def sync(self) -> None:
        rows = (
            await tenant_db
            .list_enabled_tenant_dzen_accounts()
        )

        desired: dict[int, Any] = {}

        for row in rows:
            user_id = int(row["user_id"])

            # Для обычного клиента Dzen работает только
            # при активной подписке.
            # Superadmin имеет служебный доступ без оплаты.
            is_admin = user_id in getattr(
                self.cfg,
                "admin_ids",
                set(),
            )

            if (
                not is_admin
                and not await tenant_db.is_subscription_active(
                    user_id
                )
            ):
                continue

            comments_url = str(
                row["comments_url"] or ""
            ).strip()

            if not comments_url:
                continue

            desired[user_id] = row

        # Останавливаем тех, кто больше
        # не должен работать.
        for user_id in list(
            self.workers.keys()
        ):
            if user_id not in desired:
                await self._stop_worker(
                    user_id
                )

        # Запускаем/обновляем нужных.
        for user_id, row in desired.items():
            comments_url = str(
                row["comments_url"] or ""
            ).strip()

            profile_dir = str(
                row["profile_dir"] or ""
            ).strip()

            state_file = str(
                row["state_file"] or ""
            ).strip()

            fingerprint = (
                comments_url,
                profile_dir,
                state_file,
            )

            task = self.tasks.get(user_id)

            # Если worker аварийно завершился,
            # разрешаем его перезапуск.
            if (
                task is not None
                and task.done()
            ):
                await self._stop_worker(
                    user_id
                )

            if user_id in self.workers:
                if (
                    self.fingerprints.get(
                        user_id
                    )
                    == fingerprint
                ):
                    continue

                await self._stop_worker(
                    user_id
                )

            await self._start_worker(
                user_id=user_id,
                comments_url=comments_url,
                profile_dir=profile_dir,
                state_file=state_file,
            )

    async def _start_worker(
        self,
        *,
        user_id: int,
        comments_url: str,
        profile_dir: str,
        state_file: str,
    ) -> None:
        worker = DzenCommentResponderWorker(
            gpt_client=self.gpt_client,
            cfg=self.cfg,
            tenant_user_id=user_id,
            comments_url=comments_url,
            profile_dir=profile_dir,
            state_file=state_file,
        )

        task = asyncio.create_task(
            worker.run(),
            name=f"tenant-dzen-{user_id}",
        )

        # ========================================
        # Popular comment worker
        # ========================================

        popular = None
        popular_task = None

        # Пока article_service не передан,
        # старый responder продолжает работать
        # без изменений.
        if self.article_service is not None:

            state_value = str(
                state_file or ""
            ).strip()

            if state_value:
                base_dir = Path(
                    state_value
                ).parent
            else:
                base_dir = (
                    Path("data")
                    / "tenant_dzen"
                    / f"u{user_id}"
                )

            base_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            popular_state = (
                base_dir
                / "popular_state.json"
            )

            popular_debug = (
                base_dir
                / "popular_debug"
            )

            popular_debug.mkdir(
                parents=True,
                exist_ok=True,
            )

            adapter = (
                _TenantPopularArticleAdapter(
                    self.article_service,
                    user_id,
                )
            )

            popular = (
                DzenPopularCommentWorker(
                    article_service=adapter,
                    gpt_client=self.gpt_client,
                    cfg=self.cfg,
                )
            )

            # Используем тот же Dzen-аккаунт,
            # что и responder конкретного tenant.
            popular.source.comments_url = (
                comments_url
            )

            popular.source.profile_dir = (
                profile_dir
            )

            popular.source.debug_dir = (
                popular_debug
            )

            # Отдельный state для popular worker,
            # чтобы tenant-пользователи
            # не делили состояние между собой.
            popular.state_path = (
                popular_state
            )

            popular.enabled = True
            popular.preview_enabled = False

            state = popular._load_state()

            state["enabled"] = True
            state["preview_enabled"] = False

            state.setdefault(
                "used",
                {},
            )

            state.setdefault(
                "pending_previews",
                {},
            )

            popular._migrate_state(
                state
            )

            popular._save_state(
                state
            )

            popular_task = (
                asyncio.create_task(
                    popular.run(),
                    name=(
                        f"tenant-dzen-popular-"
                        f"{user_id}"
                    ),
                )
            )

            self.popular_workers[
                user_id
            ] = popular

            self.popular_tasks[
                user_id
            ] = popular_task

            log.info(
                "Tenant popular-comment worker "
                "запущен: user=%s",
                user_id,
            )


        self.workers[user_id] = worker
        self.tasks[user_id] = task

        self.fingerprints[user_id] = (
            comments_url,
            profile_dir,
            state_file,
        )

        log.info(
            "Tenant Dzen worker запущен: "
            "user=%s url=%s",
            user_id,
            comments_url,
        )

    async def _stop_worker(
        self,
        user_id: int,
    ) -> None:
        worker = self.workers.pop(
            user_id,
            None,
        )

        task = self.tasks.pop(
            user_id,
            None,
        )

        popular = self.popular_workers.pop(
            user_id,
            None,
        )

        popular_task = self.popular_tasks.pop(
            user_id,
            None,
        )


        self.fingerprints.pop(
            user_id,
            None,
        )

        if popular is not None:
            popular.stop()

        if worker is not None:
            worker.stop()

        if task is not None:
            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception(
                    "Tenant Dzen worker "
                    "завершился с ошибкой: "
                    "user=%s",
                    user_id,
                )

        log.info(
            "Tenant Dzen worker остановлен: "
            "user=%s",
            user_id,
        )

        if popular_task is not None:
            popular_task.cancel()

            try:
                await popular_task

            except asyncio.CancelledError:
                pass

            except Exception:
                log.exception(
                    "Tenant popular worker "
                    "завершился с ошибкой: user=%s",
                    user_id,
                )
