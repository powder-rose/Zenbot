import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def app(monkeypatch, tmp_path, background, created_tasks):
    # Load real handlers with synthetic configuration and inert AI clients.
    import config
    import image_gen
    import search
    import yandex_gpt

    cfg = SimpleNamespace(
        db_path=tmp_path / "unused.sqlite",
        default_publish_times=(),
        bot_token="123456:TEST_TOKEN_ONLY",
        yc_folder_id="test",
        yc_api_key="test",
        yc_sa_key_file=None,
        admin_ids={1},
        timezone="UTC",
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(
        yandex_gpt,
        "YandexGPTClient",
        lambda **kw: SimpleNamespace(auth_header=AsyncMock()),
    )
    monkeypatch.setattr(search, "YandexSearchClient", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(image_gen, "YandexArtClient", lambda **kw: SimpleNamespace())
    module = importlib.import_module("bot")
    monkeypatch.setattr(module, "cfg", cfg)
    events = []
    failures = set()

    def action(name):
        events.append(name)
        if name in failures:
            raise RuntimeError(f"synthetic failure: {name}")

    def async_action(name):
        async def run(*args, **kwargs):
            action(name)

        return AsyncMock(side_effect=run)

    class Worker:
        def __init__(self, name):
            self.name = name
            self.started = asyncio.Event()

        def stop(self):
            action(f"{self.name}:stop")

        async def run(self):
            created_tasks.append(asyncio.current_task())
            try:
                self.started.set()
                await asyncio.Event().wait()
            finally:
                action(f"{self.name}:finished")

    workers = {
        name: Worker(name)
        for name in ("scheduler", "tenant-scheduler", "manager", "popular", "responder")
    }
    service = SimpleNamespace(close=async_action("service:close"))
    webhook = SimpleNamespace(
        start=async_action("webhook:start"), stop=async_action("webhook:stop")
    )
    bot = SimpleNamespace(
        set_my_commands=async_action("commands:set"),
        session=SimpleNamespace(close=async_action("session:close")),
    )

    def factory(name, instance):
        def create(*args, **kwargs):
            action(f"{name}:create")
            return instance

        return create

    monkeypatch.setattr(module, "Bot", factory("bot", bot))
    monkeypatch.setattr(
        module, "CloudPaymentsWebhookServer", factory("webhook", webhook)
    )
    monkeypatch.setattr(module, "ArticleService", factory("service", service))
    monkeypatch.setattr(
        module, "TenantArticleService", factory("tenant-service", SimpleNamespace())
    )
    for attribute, name in (
        ("AutoPublisher", "scheduler"),
        ("TenantScheduler", "tenant-scheduler"),
        ("TenantDzenManager", "manager"),
        ("DzenPopularCommentWorker", "popular"),
        ("DzenCommentResponderWorker", "responder"),
    ):
        monkeypatch.setattr(module, attribute, factory(name, workers[name]))
    monkeypatch.setattr(
        module, "configure_paid_multiuse", lambda **kw: action("paid:configure")
    )
    monkeypatch.setattr(module.db, "init_db", AsyncMock())
    monkeypatch.setattr(module.db, "seed_topics", AsyncMock(return_value=0))
    monkeypatch.setattr(module.db, "ensure_default_daily_schedule", AsyncMock())
    monkeypatch.setattr(module.tenant_db, "init_db", AsyncMock())
    monkeypatch.setattr(
        module.tenant_db, "migrate_default_schedules", AsyncMock(return_value=0)
    )

    tracker = background.ShutdownMiddleware()
    real_close = tracker.close

    async def close_handlers():
        action("handlers:close")
        await real_close()

    monkeypatch.setattr(tracker, "close", AsyncMock(side_effect=close_handlers))
    monkeypatch.setattr(module, "shutdown_middleware", tracker, raising=False)

    async def close_jobs():
        action("background:close")
        await background.shutdown_background_tasks()

    monkeypatch.setattr(
        module,
        "shutdown_background_tasks",
        AsyncMock(side_effect=close_jobs),
        raising=False,
    )

    async def polling(bot, **kwargs):
        await asyncio.gather(
            *(
                workers[name].started.wait()
                for name in ("scheduler", "tenant-scheduler", "manager")
            )
        )
        action("polling")
        # Match the installed aiogram default, without using its real network loop.
        if kwargs.get("close_bot_session", True):
            await bot.session.close()

    monkeypatch.setattr(module.dp, "start_polling", AsyncMock(side_effect=polling))
    monkeypatch.setattr(module.dp, "stop_polling", AsyncMock())
    return SimpleNamespace(
        module=module,
        events=events,
        failures=failures,
        workers=workers,
        service=service,
        webhook=webhook,
        bot=bot,
        tracker=tracker,
        polling=polling,
    )


@pytest.mark.asyncio
async def test_session_stays_open_until_tasks_and_services_close(app):
    await asyncio.wait_for(app.module.main(), 5)
    events = app.events
    for name in ("scheduler", "tenant-scheduler", "manager"):
        assert events.index(f"{name}:finished") < events.index("service:close")
    assert events.index("handlers:close") < events.index("background:close")
    assert events.index("background:close") < events.index("service:close")
    assert events.index("service:close") < events.index("session:close")
    assert events.index("webhook:stop") < events.index("session:close")
    app.bot.session.close.assert_awaited_once_with()
    assert app.module.dp.start_polling.call_args.kwargs["close_bot_session"] is False


def test_shutdown_middleware_is_registered(app, background):
    assert any(
        isinstance(middleware, background.ShutdownMiddleware)
        for middleware in app.module.dp.update.outer_middleware
    )


@pytest.mark.asyncio
async def test_active_update_finishes_before_resources_close(app, created_tasks):
    started = asyncio.Event()

    async def handler(event, data):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            app.events.append("handler:finished")

    async def polling(bot, **kwargs):
        task = asyncio.create_task(app.tracker(handler, object(), {}))
        created_tasks.append(task)
        await asyncio.wait_for(started.wait(), 5)
        await app.polling(bot, **kwargs)

    app.module.dp.start_polling.side_effect = polling
    await asyncio.wait_for(app.module.main(), 5)
    assert "handler:finished" in app.events
    assert app.events.index("handler:finished") < app.events.index("service:close")
    assert not app.tracker.tasks


@pytest.mark.asyncio
async def test_background_job_finishes_before_resources_close(
    app, background, created_tasks
):
    started = asyncio.Event()

    async def job():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            app.events.append("background:finished")

    async def polling(bot, **kwargs):
        created_tasks.append(background.spawn_background_task(job(), name="app-test"))
        await asyncio.wait_for(started.wait(), 5)
        await app.polling(bot, **kwargs)

    app.module.dp.start_polling.side_effect = polling
    await asyncio.wait_for(app.module.main(), 5)
    assert "background:finished" in app.events
    assert app.events.index("background:finished") < app.events.index("service:close")


@pytest.mark.asyncio
async def test_polling_error_propagates_after_cleanup(app):
    app.failures.add("polling")
    with pytest.raises(RuntimeError, match="synthetic failure: polling"):
        await asyncio.wait_for(app.module.main(), 5)
    app.service.close.assert_awaited_once_with()
    app.webhook.stop.assert_awaited_once_with()
    app.bot.session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_main_cancellation_still_cleans_resources(app, created_tasks):
    started = asyncio.Event()

    async def polling(bot, **kwargs):
        await asyncio.gather(
            *(
                app.workers[name].started.wait()
                for name in ("scheduler", "tenant-scheduler", "manager")
            )
        )
        started.set()
        await asyncio.Event().wait()

    app.module.dp.start_polling.side_effect = polling
    task = asyncio.create_task(app.module.main())
    created_tasks.append(task)
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    app.service.close.assert_awaited_once_with()
    app.webhook.stop.assert_awaited_once_with()
    app.bot.session.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "webhook:create",
        "webhook:start",
        "service:create",
        "manager:create",
        "paid:configure",
        "commands:set",
    ],
)
async def test_partial_startup_closes_acquired_resources(app, failure):
    app.failures.add(failure)
    with pytest.raises(RuntimeError, match=f"synthetic failure: {failure}"):
        await asyncio.wait_for(app.module.main(), 5)
    app.bot.session.close.assert_awaited_once_with()
    assert app.webhook.stop.await_count == (failure != "webhook:create")
    assert app.service.close.await_count == (
        failure in {"manager:create", "paid:configure", "commands:set"}
    )
    app.module.dp.start_polling.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "scheduler:stop",
        "scheduler:finished",
        "handlers:close",
        "background:close",
        "service:close",
        "webhook:stop",
    ],
)
async def test_shutdown_error_does_not_skip_remaining_cleanup(app, failure, caplog):
    app.failures.add(failure)
    await asyncio.wait_for(app.module.main(), 5)
    app.service.close.assert_awaited_once_with()
    app.webhook.stop.assert_awaited_once_with()
    app.bot.session.close.assert_awaited_once_with()
    app.tracker.close.assert_awaited_once_with()
    app.module.shutdown_background_tasks.assert_awaited_once_with()
    assert f"synthetic failure: {failure}" in caplog.text


@pytest.mark.asyncio
async def test_completed_shutdown_blocks_new_work(app, background):
    await asyncio.wait_for(app.module.main(), 5)
    handler = AsyncMock()
    await app.tracker(handler, object(), {})
    handler.assert_not_awaited()
    coroutine = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="shutting down"):
            background.spawn_background_task(coroutine, name="late-job")
    finally:
        coroutine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "cancel"])
async def test_real_dispatcher_loop_stops_with_application(app, monkeypatch, reason):
    from aiogram import Dispatcher

    dispatcher = Dispatcher()
    started, stopped = asyncio.Event(), asyncio.Event()
    before = asyncio.all_tasks()
    real_start = dispatcher.start_polling

    async def fake_network_loop(*args, **kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def start_without_os_signals(*args, **kwargs):
        return await real_start(*args, **kwargs, handle_signals=False)

    monkeypatch.setattr(dispatcher, "_polling", fake_network_loop)
    monkeypatch.setattr(dispatcher, "start_polling", start_without_os_signals)
    monkeypatch.setattr(app.module, "dp", dispatcher)
    main = asyncio.create_task(app.module.main())
    try:
        await asyncio.wait_for(started.wait(), 5)
        if reason == "cancel":
            main.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(main, 5)
        else:
            await asyncio.wait_for(dispatcher.stop_polling(), 5)
            await asyncio.wait_for(main, 5)
        assert stopped.is_set(), "The dispatcher left its network loop running"
        assert not (asyncio.all_tasks() - before), "Dispatcher left pending tasks"
        app.bot.session.close.assert_awaited_once_with()
    finally:
        remaining = asyncio.all_tasks() - before
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
