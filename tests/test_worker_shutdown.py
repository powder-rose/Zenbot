import asyncio

import pytest

from dzen_comment_responder import DzenCommentResponderWorker
from dzen_popular_comments import DzenPopularCommentWorker


class ObservedEvent(asyncio.Event):
    """Запоминает задачи, которые начали ждать этого события."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.waiters = set()

    async def wait(self):
        self.waiters.add(asyncio.current_task())
        self.started.set()
        return await super().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_class",
    [DzenCommentResponderWorker, DzenPopularCommentWorker],
    ids=["comment-responder", "popular-comments"],
)
@pytest.mark.parametrize("exit_reason", ["stop", "wake", "cancel", "timeout"])
async def test_wait_finishes_without_leftover_tasks(worker_class, exit_reason):
    # Проверяем только ожидание сигналов. Обычный запуск обработчика
    # не выполняем: браузер, публикации и рабочие данные здесь не нужны.
    worker = object.__new__(worker_class)
    worker._stop = ObservedEvent()
    worker._wake = ObservedEvent()

    parent = asyncio.create_task(worker._wait_for_stop_or_wake())

    async def wait_until_ready():
        await worker._stop.started.wait()
        await worker._wake.started.wait()

    try:
        # Сначала убеждаемся, что оба ожидания действительно начались.
        await asyncio.wait_for(wait_until_ready(), timeout=5)

        if exit_reason == "stop":
            worker._stop.set()
            await asyncio.wait_for(parent, timeout=5)

        elif exit_reason == "wake":
            worker._wake.set()
            await asyncio.wait_for(parent, timeout=5)

        elif exit_reason == "cancel":
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await parent

        elif exit_reason == "timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(parent, timeout=0.05)

        child_tasks = worker._stop.waiters | worker._wake.waiters
        unfinished = [task for task in child_tasks if not task.done()]

        assert not unfinished, (
            f"{worker_class.__name__}: after {exit_reason}, "
            f"{len(unfinished)} background task(s) are still running"
        )

    finally:
        # Даже при обнаружении ошибки тест убирает созданные задачи,
        # чтобы они не мешали следующим проверкам.
        created_tasks = {parent} | worker._stop.waiters | worker._wake.waiters

        for task in created_tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*created_tasks, return_exceptions=True)
