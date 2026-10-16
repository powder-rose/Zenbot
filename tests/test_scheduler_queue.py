import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(params=["admin", "tenant"])
async def queue_case(request, tmp_path, monkeypatch):
    import db
    import tenant_db

    path = tmp_path / "scheduler-queue.sqlite"
    clock = SimpleNamespace(current=datetime(2030, 1, 1, 6, tzinfo=UTC))
    for database in (db, tenant_db):
        monkeypatch.setattr(database, "DB_PATH", path)
        monkeypatch.setattr(database, "_DB_LOCK", asyncio.Lock())
        await database.init_db(path)
    monkeypatch.setattr(
        tenant_db, "_now", lambda: clock.current.isoformat(timespec="seconds")
    )
    records = []
    ticks = []
    advances = []
    failures = set()
    after_send = []
    tick_actions = []
    configured_users = set()
    active = 0
    max_active = 0

    class Clock:
        @classmethod
        def now(cls, tz):
            return clock.current.astimezone(tz)

    async def publish(user_id=None, *, trigger):
        nonlocal active, max_active
        assert trigger == "auto"
        active += 1
        max_active = max(max_active, active)
        try:
            if request.param == "admin":
                slot = await db.get_setting("last_auto_slot")
            else:
                slot = await tenant_db.get_setting(user_id, "last_auto_slot")
            records.append(slot)
            index = len(records) - 1
            if index < len(advances):
                # Advance a synthetic clock instead of waiting several minutes
                # for real AI generation, Telegram or Dzen.
                clock.current += advances[index]
            await asyncio.sleep(0)
            if after_send:
                await after_send[0](len(records))
            if index in failures:
                raise RuntimeError("synthetic publication failure")
            return {"status": "ok", "telegram": "published"}
        finally:
            active -= 1

    service = SimpleNamespace(
        publish_random_topic=publish,
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    cfg = SimpleNamespace(timezone="Europe/Moscow", admin_ids={1})
    if request.param == "admin":
        import scheduler as module

        worker = module.AutoPublisher(service, cfg)
    else:
        import tenant_scheduler as module

        worker = module.TenantScheduler(service, cfg)
    monkeypatch.setattr(module, "datetime", Clock)

    async def tick():
        # Model ordinary idle timer wakeups without waiting 20 seconds.
        # Record the work completed before each idle pause.
        ticks.append(tuple(records))
        await asyncio.sleep(0)
        if tick_actions:
            await tick_actions.pop(0)()
        if len(ticks) >= 4:
            worker.stop()

    monkeypatch.setattr(worker._stop, "wait", tick)

    async def configure(start, times, durations, *, same_user=False):
        clock.current = start
        advances.extend(durations)
        for index, publish_time in enumerate(times):
            if request.param == "admin":
                assert await db.add_schedule_time(publish_time)
            else:
                # Separate users make the ordering of adjacent times visible
                # without assuming that they can publish simultaneously.
                user_id = 1001 if same_user else 1001 + index
                configured_users.add(user_id)
                await tenant_db.activate_subscription(
                    user_id,
                    expires_at=start + timedelta(days=30),
                    source="test",
                )
                await tenant_db.ensure_defaults(user_id, (publish_time,), ())

    async def run():
        await asyncio.wait_for(worker.run(), timeout=5)

    async def set_enabled(enabled):
        if request.param == "admin":
            await db.set_auto_publish_enabled(enabled)
        else:
            for user_id in configured_users:
                await tenant_db.set_setting(
                    user_id, "auto_publish_enabled", "1" if enabled else "0"
                )

    return SimpleNamespace(
        configure=configure,
        run=run,
        records=records,
        ticks=ticks,
        max_active=lambda: max_active,
        failures=failures,
        after_send=after_send,
        tick_actions=tick_actions,
        set_enabled=set_enabled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "times", "durations", "expected"),
    [
        (
            datetime(2030, 1, 1, 6, tzinfo=UTC),
            ("09:00", "09:01"),
            (timedelta(minutes=2),),
            ("2030-01-01|09:00", "2030-01-01|09:01"),
        ),
        (
            datetime(2030, 1, 1, 6, tzinfo=UTC),
            ("09:00", "09:01", "09:02"),
            (timedelta(minutes=3),),
            (
                "2030-01-01|09:00",
                "2030-01-01|09:01",
                "2030-01-01|09:02",
            ),
        ),
        (
            datetime(2030, 1, 1, 20, 59, tzinfo=UTC),
            ("23:59", "00:00"),
            (timedelta(minutes=2),),
            ("2030-01-01|23:59", "2030-01-02|00:00"),
        ),
        (
            datetime(2030, 1, 1, 6, tzinfo=UTC),
            ("09:00", "09:01", "09:02", "09:03"),
            (timedelta(minutes=2), timedelta(minutes=2)),
            (
                "2030-01-01|09:00",
                "2030-01-01|09:01",
                "2030-01-01|09:02",
                "2030-01-01|09:03",
            ),
        ),
    ],
    ids=["one-waiting", "several-waiting", "midnight", "new-slots-while-draining"],
)
async def test_due_slots_wait_and_publish_in_order(
    queue_case, start, times, durations, expected
):
    case = queue_case
    await case.configure(start, times, durations)
    await case.run()

    assert case.records == list(expected), (
        "Запуски, наступившие во время предыдущей публикации, потеряны "
        "или отправлены не по порядку"
    )
    assert case.max_active() == 1, "Ожидающие публикации начали выполняться параллельно"
    assert case.ticks and case.ticks[0] == expected, (
        "Перед ожидающей публикацией добавлена обычная пауза проверки расписания"
    )


@pytest.mark.asyncio
async def test_repeated_ticks_within_one_minute_do_not_duplicate_publication(
    queue_case,
):
    case = queue_case
    expected = ["2030-01-01|09:00"]
    await case.configure(
        datetime(2030, 1, 1, 6, tzinfo=UTC),
        ("09:00",),
        (timedelta(seconds=10),),
    )
    await case.run()

    assert case.records == expected
    assert case.max_active() == 1


@pytest.mark.asyncio
async def test_same_user_receives_each_waiting_slot_once(queue_case):
    case = queue_case
    expected = [
        "2030-01-01|09:00",
        "2030-01-01|09:01",
        "2030-01-01|09:02",
    ]
    await case.configure(
        datetime(2030, 1, 1, 6, tzinfo=UTC),
        ("09:00", "09:01", "09:02"),
        (timedelta(minutes=3),),
        same_user=True,
    )
    await case.run()

    assert case.records == expected
    assert case.max_active() == 1
    assert case.ticks[0] == tuple(expected)


@pytest.mark.asyncio
async def test_failed_attempt_is_not_repeated_and_next_slot_can_run(queue_case):
    case = queue_case
    expected = ["2030-01-01|09:00", "2030-01-01|09:01"]
    await case.configure(
        datetime(2030, 1, 1, 6, tzinfo=UTC),
        ("09:00", "09:01"),
        (timedelta(minutes=2),),
    )
    case.failures.add(0)
    await case.run()

    assert case.records == expected
    assert case.max_active() == 1
    assert case.ticks[0] == tuple(expected)


@pytest.mark.asyncio
async def test_disabled_auto_publish_prevents_waiting_publication(queue_case):
    case = queue_case
    await case.configure(
        datetime(2030, 1, 1, 6, tzinfo=UTC),
        ("09:00", "09:01"),
        (timedelta(minutes=2),),
    )

    async def disable_after_first(call_count):
        if call_count == 1:
            await case.set_enabled(False)

    case.after_send.append(disable_after_first)
    await case.run()

    assert case.records == ["2030-01-01|09:00"]
    assert case.max_active() == 1


@pytest.mark.asyncio
async def test_enabling_auto_in_current_minute_still_allows_current_slot(queue_case):
    case = queue_case
    await case.configure(
        datetime(2030, 1, 1, 6, tzinfo=UTC),
        ("09:00",),
        (timedelta(seconds=10),),
    )
    await case.set_enabled(False)

    async def enable():
        await case.set_enabled(True)

    case.tick_actions.append(enable)
    await case.run()

    assert case.ticks[0] == ()
    assert case.records == ["2030-01-01|09:00"]
    assert case.max_active() == 1


@pytest.mark.asyncio
async def test_fresh_start_does_not_replay_time_before_scheduler_started(queue_case):
    case = queue_case
    await case.configure(
        datetime(2030, 1, 1, 6, 2, tzinfo=UTC),
        ("09:00", "09:01", "09:02"),
        (),
    )
    await case.run()

    assert case.records == ["2030-01-01|09:02"]
    assert case.max_active() == 1
