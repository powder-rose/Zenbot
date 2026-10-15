import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def tracker(background):
    return background.ShutdownMiddleware()


async def start_handler(tracker, created_tasks, cleanup=None):
    started = asyncio.Event()

    async def handler(event, data):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            if cleanup:
                await cleanup()

    task = asyncio.create_task(tracker(handler, object(), {}))
    created_tasks.append(task)
    await asyncio.wait_for(started.wait(), 5)
    return task


@pytest.mark.asyncio
async def test_handler_is_tracked_and_result_preserved(tracker):
    event, data = object(), {"example": 1}

    async def handler(received_event, received_data):
        assert received_event is event and received_data is data
        assert asyncio.current_task() in tracker.tasks
        return "handled"

    assert await tracker(handler, event, data) == "handled"
    assert not tracker.tasks


@pytest.mark.asyncio
async def test_failed_handler_is_removed_and_error_propagates(tracker):
    handler = AsyncMock(side_effect=RuntimeError("synthetic handler error"))
    with pytest.raises(RuntimeError, match="synthetic handler error"):
        await tracker(handler, object(), {})
    assert not tracker.tasks


@pytest.mark.asyncio
async def test_stop_blocks_new_updates(tracker):
    tracker.stop()
    handler = AsyncMock()
    assert await tracker(handler, object(), {}) is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_cancels_all_active_handlers(tracker, created_tasks):
    first = await start_handler(tracker, created_tasks)
    second = await start_handler(tracker, created_tasks)
    await asyncio.wait_for(tracker.close(), 5)
    assert first.cancelled() and second.cancelled()
    assert not tracker.tasks


@pytest.mark.asyncio
async def test_close_waits_for_handler_cleanup(tracker, created_tasks):
    entered, release = asyncio.Event(), asyncio.Event()

    async def cleanup():
        entered.set()
        await release.wait()

    await start_handler(tracker, created_tasks, cleanup)
    closer = asyncio.create_task(tracker.close())
    created_tasks.append(closer)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not closer.done()
        release.set()
        await asyncio.wait_for(closer, 5)
        assert not tracker.tasks
    finally:
        release.set()


@pytest.mark.asyncio
async def test_close_preserves_unrelated_task(tracker, created_tasks):
    owned = await start_handler(tracker, created_tasks)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    created_tasks.append(unrelated)
    await tracker.close()
    assert owned.cancelled()
    assert not unrelated.done()
