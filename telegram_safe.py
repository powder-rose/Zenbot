from __future__ import annotations

import asyncio
import logging
from inspect import iscoroutine
from typing import Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
)
from aiogram.types import CallbackQuery


log = logging.getLogger(__name__)


_BACKGROUND_TASKS: set[
    asyncio.Task[Any]
] = set()
_BACKGROUND_TASKS_CLOSING = False


def spawn_background_task(
    awaitable: Awaitable[Any],
    *,
    name: str,
) -> asyncio.Task[Any]:
    """
    Запускает долгую операцию отдельно от
    Telegram update handler и удерживает ссылку
    на Task до её завершения.
    """

    if _BACKGROUND_TASKS_CLOSING:
        if iscoroutine(awaitable):
            awaitable.close()
        raise RuntimeError("Bot is shutting down; new background tasks are disabled")

    task = asyncio.create_task(
        awaitable,
        name=name,
    )

    _BACKGROUND_TASKS.add(
        task
    )

    def done(
        finished: asyncio.Task[Any],
    ) -> None:
        _BACKGROUND_TASKS.discard(
            finished
        )

        if finished.cancelled():
            return

        try:
            exc = finished.exception()
        except asyncio.CancelledError:
            return

        if exc is not None:
            log.error(
                "Background task failed: "
                "name=%s error=%r",
                name,
                exc,
            )

    task.add_done_callback(
        done
    )

    return task



async def shutdown_background_tasks() -> None:
    """Stop accepting jobs, then cancel and await the registered jobs."""
    global _BACKGROUND_TASKS_CLOSING
    _BACKGROUND_TASKS_CLOSING = True
    tasks = tuple(_BACKGROUND_TASKS)
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        # The callback installed by spawn_background_task reports failures.
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _BACKGROUND_TASKS.difference_update(task for task in tasks if task.done())


class ShutdownMiddleware(BaseMiddleware):
    """Keep active Telegram handlers alive until their cleanup is complete."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    def stop(self) -> None:
        self._closing = True

    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        if self._closing:
            return None
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            if task is not None:
                self.tasks.discard(task)

    async def close(self) -> None:
        self.stop()
        tasks = tuple(self.tasks - {asyncio.current_task()})
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                log.error(
                    "Telegram handler failed during shutdown: %s",
                    task.get_name(),
                    exc_info=(type(result), result, result.__traceback__),
                )


async def safe_callback_answer(
    call: CallbackQuery,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """
    Подтверждает Telegram callback без падения
    всего обработчика.

    Callback может протухнуть либо Telegram API
    временно не ответить. Это не должно отменять
    основную операцию пользователя.
    """

    try:
        await call.answer(
            *args,
            **kwargs,
        )

        return True

    except TelegramBadRequest as exc:
        message = str(
            exc
        ).lower()

        if (
            "query is too old" in message
            or "query id is invalid" in message
            or "response timeout expired" in message
        ):
            log.warning(
                "Telegram callback уже недействителен: %s",
                exc,
            )

            return False

        raise

    except TelegramNetworkError as exc:
        log.warning(
            "Telegram callback network error: %s",
            exc,
        )

        return False
