import asyncio
import inspect

import pytest


async def start_job(background, created_tasks, cleanup=None):
    started = asyncio.Event()

    async def job():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            if cleanup:
                await cleanup()

    task = background.spawn_background_task(job(), name="test-background-job")
    created_tasks.append(task)
    await asyncio.wait_for(started.wait(), 5)
    return task


@pytest.mark.asyncio
async def test_finished_job_is_removed_from_registry(background):
    async def job():
        return 42

    task = background.spawn_background_task(job(), name="finished")
    assert task in background._BACKGROUND_TASKS
    assert await task == 42
    assert task not in background._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_failed_job_is_reported(background, caplog):
    async def job():
        raise RuntimeError("synthetic background failure")

    task = background.spawn_background_task(job(), name="failed")
    with pytest.raises(RuntimeError, match="synthetic background failure"):
        await task
    assert task not in background._BACKGROUND_TASKS
    assert "synthetic background failure" in caplog.text


@pytest.mark.asyncio
async def test_empty_registry_can_shutdown(background):
    await background.shutdown_background_tasks()
    assert not background._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_shutdown_stops_all_jobs(background, created_tasks):
    first = await start_job(background, created_tasks)
    second = await start_job(background, created_tasks)
    await asyncio.wait_for(background.shutdown_background_tasks(), 5)
    assert first.cancelled() and second.cancelled()
    assert not background._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_shutdown_waits_for_job_cleanup(background, created_tasks):
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def cleanup():
        entered.set()
        await release.wait()
        finished.set()

    await start_job(background, created_tasks, cleanup)
    try:
        closer = asyncio.create_task(background.shutdown_background_tasks())
        created_tasks.append(closer)
        await asyncio.wait_for(entered.wait(), 5)
        assert not closer.done()
        release.set()
        await asyncio.wait_for(closer, 5)
        assert finished.is_set()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_shutdown_leaves_unrelated_tasks_running(background, created_tasks):
    owned = await start_job(background, created_tasks)
    other = asyncio.create_task(asyncio.Event().wait())
    created_tasks.append(other)
    await background.shutdown_background_tasks()
    assert owned.cancelled()
    assert not other.done()


@pytest.mark.asyncio
async def test_shutdown_can_be_called_twice(background, created_tasks):
    task = await start_job(background, created_tasks)
    await background.shutdown_background_tasks()
    await background.shutdown_background_tasks()
    assert task.cancelled()
    assert task.cancelling() == 1


@pytest.mark.asyncio
async def test_cleanup_error_does_not_leave_other_jobs_running(
    background, created_tasks, caplog
):
    async def cleanup():
        raise RuntimeError("synthetic cleanup failure")

    failed = await start_job(background, created_tasks, cleanup)
    other = await start_job(background, created_tasks)
    await background.shutdown_background_tasks()
    assert failed.done()
    assert isinstance(failed.exception(), RuntimeError)
    assert other.cancelled()
    assert "synthetic cleanup failure" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_rejects_new_job_and_closes_coroutine(background):
    await background.shutdown_background_tasks()

    async def job():
        pytest.fail("Rejected job must never run")

    coroutine = job()
    try:
        with pytest.raises(RuntimeError, match="shutting down"):
            background.spawn_background_task(coroutine, name="too-late")
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert not background._BACKGROUND_TASKS
    finally:
        coroutine.close()


@pytest.mark.asyncio
async def test_job_cleanup_cannot_spawn_more_jobs(background, created_tasks):
    rejected = asyncio.Event()

    async def replacement():
        pytest.fail("A replacement job must not run during shutdown")

    async def cleanup():
        coroutine = replacement()
        try:
            with pytest.raises(RuntimeError, match="shutting down"):
                background.spawn_background_task(coroutine, name="replacement")
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
            rejected.set()
        finally:
            coroutine.close()

    await start_job(background, created_tasks, cleanup)
    await background.shutdown_background_tasks()
    assert rejected.is_set()
