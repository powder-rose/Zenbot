import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest.fixture
def service(monkeypatch):
    # Import configuration without reading the developer's .env file.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    from article_service import ArticleService

    # Avoid real clients, database access and browser/profile creation.
    instance = object.__new__(ArticleService)
    instance.background_tasks = set()
    instance.telegram_web = SimpleNamespace(close=AsyncMock())
    return instance


@pytest_asyncio.fixture
async def tasks():
    created = []
    yield created
    for task in created:
        if not task.done():
            task.cancel()
    await asyncio.gather(*created, return_exceptions=True)


async def start_background(service, tasks, cleanup=None):
    started = asyncio.Event()

    async def run():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            if cleanup is not None:
                await cleanup()

    task = asyncio.create_task(run())
    tasks.append(task)
    service.background_tasks.add(task)
    task.add_done_callback(service.background_tasks.discard)
    await asyncio.wait_for(started.wait(), timeout=5)
    return task


@pytest.mark.asyncio
async def test_close_without_background_tasks_closes_browser(service):
    await asyncio.wait_for(service.close(), timeout=5)
    service.telegram_web.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_stops_task_before_closing_browser(service, tasks):
    events = []

    async def cleanup():
        events.append("task-stopped")

    async def close_browser():
        events.append("browser-closed")

    service.telegram_web.close.side_effect = close_browser
    task = await start_background(service, tasks, cleanup)
    await asyncio.wait_for(service.close(), timeout=5)

    assert task.cancelled()
    assert events == ["task-stopped", "browser-closed"]
    assert not service.background_tasks


@pytest.mark.asyncio
async def test_close_stops_all_registered_tasks(service, tasks):
    first = await start_background(service, tasks)
    second = await start_background(service, tasks)

    await asyncio.wait_for(service.close(), timeout=5)

    assert first.cancelled()
    assert second.cancelled()
    assert not service.background_tasks
    service.telegram_web.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_waits_for_async_cleanup_before_browser(service, tasks):
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await allow_cleanup.wait()
        cleanup_finished.set()

    await start_background(service, tasks, cleanup)
    closer = asyncio.create_task(service.close())
    ready = asyncio.create_task(cleanup_started.wait())
    tasks.extend([closer, ready])

    try:
        await asyncio.wait(
            {closer, ready}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert cleanup_started.is_set(), "Shutdown skipped task cleanup"
        assert not closer.done(), "Shutdown returned before cleanup finished"
        service.telegram_web.close.assert_not_awaited()

        allow_cleanup.set()
        await asyncio.wait_for(closer, timeout=5)

        assert cleanup_finished.is_set()
        service.telegram_web.close.assert_awaited_once_with()
    finally:
        allow_cleanup.set()


@pytest.mark.asyncio
async def test_close_preserves_completed_task_result(service, tasks):
    async def completed():
        return "already-finished"

    task = asyncio.create_task(completed())
    tasks.append(task)
    await task
    service.background_tasks.add(task)

    await asyncio.wait_for(service.close(), timeout=5)

    assert task.result() == "already-finished"
    assert not service.background_tasks
    service.telegram_web.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cleanup_failure_is_reported_and_other_tasks_stop(service, tasks, caplog):
    async def failed_cleanup():
        raise RuntimeError("synthetic cleanup failure")

    failed = await start_background(service, tasks, failed_cleanup)
    other = await start_background(service, tasks)

    await asyncio.wait_for(service.close(), timeout=5)

    assert failed.done()
    assert isinstance(failed.exception(), RuntimeError)
    assert other.cancelled()
    assert "synthetic cleanup failure" in caplog.text
    service.telegram_web.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_accepts_already_cancelled_task(service, tasks):
    task = await start_background(service, tasks)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    service.background_tasks.add(task)

    await asyncio.wait_for(service.close(), timeout=5)

    assert task.cancelled()
    assert not service.background_tasks
    service.telegram_web.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_browser_failure_does_not_leave_background_tasks(service, tasks):
    task = await start_background(service, tasks)
    service.telegram_web.close.side_effect = RuntimeError("synthetic browser error")

    with pytest.raises(RuntimeError, match="synthetic browser error"):
        await asyncio.wait_for(service.close(), timeout=5)

    assert task.cancelled()
    assert not service.background_tasks


@pytest.mark.asyncio
async def test_repeated_close_does_not_cancel_finished_task_again(service, tasks):
    task = await start_background(service, tasks)

    await asyncio.wait_for(service.close(), timeout=5)
    await asyncio.wait_for(service.close(), timeout=5)

    assert task.cancelled()
    assert task.cancelling() == 1
    assert not service.background_tasks


@pytest.mark.asyncio
async def test_close_does_not_stop_unrelated_task(service, tasks):
    owned = await start_background(service, tasks)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    tasks.append(unrelated)

    await asyncio.wait_for(service.close(), timeout=5)

    assert owned.cancelled()
    assert not unrelated.done()
