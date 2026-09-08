from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
)
from aiogram.types import CallbackQuery


log = logging.getLogger(__name__)


_BACKGROUND_TASKS: set[
    asyncio.Task[Any]
] = set()


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
