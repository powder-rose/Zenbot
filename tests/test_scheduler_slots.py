import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def tenant_case(tmp_path, monkeypatch):
    import tenant_db

    path = tmp_path / "scheduler.sqlite"
    now = datetime(2030, 1, 1, 6, tzinfo=UTC)
    slot = "2030-01-01|09:00"
    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    monkeypatch.setattr(tenant_db, "_DB_LOCK", asyncio.Lock())
    monkeypatch.setattr(tenant_db, "_now", lambda: now.isoformat(timespec="seconds"))
    await tenant_db.init_db(path)

    async def add_user(user_id=1001, *, times=("09:00",), expires_at=None):
        await tenant_db.activate_subscription(
            user_id,
            expires_at=expires_at or now + timedelta(days=30),
            source="test",
        )
        await tenant_db.ensure_defaults(user_id, times, ())

    async def execute(sql, parameters=()):
        async with aiosqlite.connect(path) as connection:
            await connection.execute(sql, parameters)
            await connection.commit()

    return SimpleNamespace(
        database=tenant_db,
        path=path,
        now=now,
        slot=slot,
        add_user=add_user,
        execute=execute,
    )


@pytest_asyncio.fixture(params=["admin", "tenant"])
async def scheduler_case(request, tenant_case, monkeypatch):
    case = tenant_case
    service = SimpleNamespace(
        publish_random_topic=AsyncMock(
            return_value={"status": "ok", "telegram": "published"}
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    cfg = SimpleNamespace(timezone="Europe/Moscow", admin_ids={1})

    class Clock:
        @classmethod
        def now(cls, tz):
            return case.now.astimezone(tz)

    if request.param == "admin":
        import db
        import scheduler as module

        monkeypatch.setattr(db, "DB_PATH", case.path)
        monkeypatch.setattr(db, "_DB_LOCK", asyncio.Lock())
        await db.init_db(case.path)
        await db.add_schedule_time("09:00")
        worker = module.AutoPublisher(service, cfg)

        async def last_slot():
            return await db.get_setting("last_auto_slot")

    else:
        import tenant_scheduler as module

        await case.add_user()
        worker = module.TenantScheduler(service, cfg)

        async def last_slot():
            return await case.database.get_setting(1001, "last_auto_slot")

    monkeypatch.setattr(module, "datetime", Clock)
    return SimpleNamespace(
        worker=worker,
        service=service,
        slot=case.slot,
        last_slot=last_slot,
        kind=request.param,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition",
    ["active", "expired", "revoked", "auto-off", "another-time", "disabled-time"],
)
async def test_only_eligible_users_are_claimed(tenant_case, condition):
    case = tenant_case
    expires_at = case.now - timedelta(seconds=1) if condition == "expired" else None
    times = ("09:01",) if condition == "another-time" else ("09:00",)
    await case.add_user(expires_at=expires_at, times=times)
    if condition == "revoked":
        await case.database.revoke_subscription(1001)
    elif condition == "auto-off":
        await case.database.set_setting(1001, "auto_publish_enabled", "0")
    elif condition == "disabled-time":
        await case.execute(
            "UPDATE tenant_schedule SET enabled=0 WHERE user_id=?", (1001,)
        )

    claimed = await case.database.claim_due_users("09:00", case.slot)

    assert claimed == ([1001] if condition == "active" else [])
    assert await case.database.get_setting(1001, "last_auto_slot") == (
        case.slot if condition == "active" else ""
    )


@pytest.mark.asyncio
async def test_same_slot_cannot_be_claimed_twice(tenant_case):
    case = tenant_case
    await case.add_user()

    assert await case.database.claim_due_users("09:00", case.slot) == [1001]
    assert await case.database.claim_due_users("09:00", case.slot) == []


@pytest.mark.asyncio
async def test_concurrent_claims_do_not_duplicate_users(tenant_case):
    case = tenant_case
    await case.add_user(1001)
    await case.add_user(1002)

    first, second = await asyncio.gather(
        case.database.claim_due_users("09:00", case.slot),
        case.database.claim_due_users("09:00", case.slot),
    )

    assert sorted(first + second) == [1001, 1002]
    assert not set(first).intersection(second)


@pytest.mark.asyncio
async def test_same_time_on_next_day_is_a_new_slot(tenant_case):
    case = tenant_case
    await case.add_user()

    assert await case.database.claim_due_users("09:00", case.slot) == [1001]
    assert await case.database.claim_due_users("09:00", "2030-01-02|09:00") == [1001]


@pytest.mark.asyncio
async def test_failed_batch_claim_leaves_all_users_available(tenant_case):
    case = tenant_case
    await case.add_user(1001)
    await case.add_user(1002)
    await case.execute(
        "CREATE TRIGGER synthetic_claim_failure BEFORE INSERT ON tenant_settings "
        "WHEN NEW.user_id=1002 AND NEW.key='last_auto_slot' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic claim failure'); END"
    )

    with pytest.raises(aiosqlite.IntegrityError, match="synthetic claim failure"):
        await case.database.claim_due_users("09:00", case.slot)

    for user_id in (1001, 1002):
        assert await case.database.get_setting(user_id, "last_auto_slot") == ""
    await case.execute("DROP TRIGGER synthetic_claim_failure")
    assert sorted(await case.database.claim_due_users("09:00", case.slot)) == [
        1001,
        1002,
    ]


@pytest.mark.asyncio
async def test_repeated_ticks_publish_once_in_configured_timezone(
    scheduler_case, monkeypatch
):
    case = scheduler_case
    ticks = 0

    async def tick():
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            case.worker.stop()

    # Two timer wakeups without a real 20-second delay. The clock stays at
    # 06:00 UTC, which must match the configured 09:00 in Europe/Moscow.
    monkeypatch.setattr(case.worker._stop, "wait", tick)
    await asyncio.wait_for(case.worker.run(), timeout=5)

    case.service.publish_random_topic.assert_awaited_once()
    assert case.service.publish_random_topic.call_args.kwargs == {"trigger": "auto"}
    assert await case.last_slot() == case.slot
    case.service.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_while_scheduler_waits_propagates(
    scheduler_case, created_tasks
):
    case = scheduler_case

    class ObservedStop(asyncio.Event):
        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()

        async def wait(self):
            self.waiting.set()
            return await super().wait()

    case.worker._stop = ObservedStop()
    task = asyncio.create_task(case.worker.run())
    created_tasks.append(task)
    await asyncio.wait_for(case.worker._stop.waiting.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_one_user_failure_does_not_prevent_other_users(tenant_case, monkeypatch):
    import tenant_scheduler

    case = tenant_case
    await case.add_user(1001)
    await case.add_user(1002)
    attempted = []

    class Clock:
        @classmethod
        def now(cls, tz):
            return case.now.astimezone(tz)

    async def publish(user_id, *, trigger):
        assert trigger == "auto"
        attempted.append(user_id)
        if len(attempted) == 1:
            raise RuntimeError("synthetic generation failure")
        return {"status": "ok"}

    service = SimpleNamespace(publish_random_topic=publish)
    worker = tenant_scheduler.TenantScheduler(
        service, SimpleNamespace(timezone="Europe/Moscow")
    )

    async def tick():
        worker.stop()

    monkeypatch.setattr(tenant_scheduler, "datetime", Clock)
    monkeypatch.setattr(worker._stop, "wait", tick)
    await asyncio.wait_for(worker.run(), timeout=5)

    assert sorted(attempted) == [1001, 1002]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption", ["stop-after-claim", "stop-during-first", "cancel-during-first"]
)
async def test_unstarted_users_keep_their_slot_after_interruption(
    tenant_case, monkeypatch, created_tasks, interruption
):
    import tenant_scheduler

    case = tenant_case
    users = {1001, 1002}
    for user_id in sorted(users):
        await case.add_user(user_id)
    attempted = set()
    publishing = asyncio.Event()
    finish_publication = asyncio.Event()

    class Clock:
        @classmethod
        def now(cls, tz):
            return case.now.astimezone(tz)

    async def publish(user_id, *, trigger):
        assert trigger == "auto"
        attempted.add(user_id)
        publishing.set()
        await finish_publication.wait()
        return {"status": "ok"}

    service = SimpleNamespace(publish_random_topic=publish)
    worker = tenant_scheduler.TenantScheduler(
        service, SimpleNamespace(timezone="Europe/Moscow")
    )
    monkeypatch.setattr(tenant_scheduler, "datetime", Clock)
    original_claim = case.database.claim_due_users

    if interruption == "stop-after-claim":

        async def claim_then_stop(*args, **kwargs):
            claimed = await original_claim(*args, **kwargs)
            worker.stop()
            return claimed

        monkeypatch.setattr(case.database, "claim_due_users", claim_then_stop)
        await asyncio.wait_for(worker.run(), timeout=5)
    else:
        task = asyncio.create_task(worker.run())
        created_tasks.append(task)
        await asyncio.wait_for(publishing.wait(), timeout=5)
        if interruption == "stop-during-first":
            worker.stop()
            finish_publication.set()
            await asyncio.wait_for(task, timeout=5)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    # Only jobs which never entered the publication service can be retried.
    # A started job keeps its marker even after cancellation: its send outcome
    # may be unknown, so this test must not authorize automatic resending.
    available = set(await original_claim("09:00", case.slot))
    assert available == users - attempted, (
        "Пользователи потеряли запуск, хотя их публикации ещё не начинались"
    )
    assert not available.intersection(attempted), (
        "Начатая публикация снова доступна для автоматической отправки"
    )


@pytest.mark.asyncio
async def test_release_preserves_newer_slot_and_unrelated_users(tenant_case):
    case = tenant_case
    for user_id in (1001, 1002, 1003):
        await case.add_user(user_id)
    await case.database.claim_due_users("09:00", case.slot)
    newer_slot = "2030-01-01|09:01"
    await case.database.set_setting(1001, "last_auto_slot", newer_slot)

    await case.database.release_unstarted_auto_slots((1001, 1002), case.slot)

    assert await case.database.get_setting(1001, "last_auto_slot") == newer_slot
    assert await case.database.get_setting(1002, "last_auto_slot") == ""
    assert await case.database.get_setting(1003, "last_auto_slot") == case.slot


@pytest.mark.asyncio
async def test_failed_release_rolls_back_whole_batch(tenant_case):
    case = tenant_case
    for user_id in (1001, 1002):
        await case.add_user(user_id)
    await case.database.claim_due_users("09:00", case.slot)
    await case.execute(
        "CREATE TRIGGER synthetic_release_failure BEFORE UPDATE ON tenant_settings "
        "WHEN NEW.user_id=1002 AND NEW.key='last_auto_slot' AND NEW.value='' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic release failure'); END"
    )

    with pytest.raises(aiosqlite.IntegrityError, match="synthetic release failure"):
        await case.database.release_unstarted_auto_slots((1001, 1002), case.slot)
    for user_id in (1001, 1002):
        assert await case.database.get_setting(user_id, "last_auto_slot") == case.slot

    await case.execute("DROP TRIGGER synthetic_release_failure")
    await case.database.release_unstarted_auto_slots((1001, 1002), case.slot)
    assert sorted(await case.database.claim_due_users("09:00", case.slot)) == [
        1001,
        1002,
    ]


@pytest.mark.asyncio
async def test_cancellation_during_claim_releases_committed_unstarted_batch(
    tenant_case, monkeypatch, created_tasks
):
    from tenant_scheduler import TenantScheduler

    case = tenant_case
    for user_id in (1001, 1002):
        await case.add_user(user_id)
    service = SimpleNamespace(publish_random_topic=AsyncMock())
    worker = TenantScheduler(service, SimpleNamespace(timezone="Europe/Moscow"))
    committed = asyncio.Event()
    finish_claim = asyncio.Event()
    original_claim = case.database.claim_due_users

    async def delayed_claim(*args, **kwargs):
        users = await original_claim(*args, **kwargs)
        committed.set()
        await finish_claim.wait()
        return users

    monkeypatch.setattr(case.database, "claim_due_users", delayed_claim)
    task = asyncio.create_task(worker._publish_due_users("09:00", case.slot))
    created_tasks.append(task)
    try:
        await asyncio.wait_for(committed.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Отмена оставила незавершённый захват запусков"
    finally:
        finish_claim.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    service.publish_random_topic.assert_not_awaited()
    assert sorted(await original_claim("09:00", case.slot)) == [1001, 1002]


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_unstarted_slot_release(
    tenant_case, monkeypatch, created_tasks
):
    from tenant_scheduler import TenantScheduler

    case = tenant_case
    for user_id in (1001, 1002):
        await case.add_user(user_id)
    publishing = asyncio.Event()
    releasing = asyncio.Event()
    finish_release = asyncio.Event()
    attempted = set()

    async def publish(user_id, *, trigger):
        attempted.add(user_id)
        publishing.set()
        await asyncio.Event().wait()

    original_release = case.database.release_unstarted_auto_slots

    async def delayed_release(*args, **kwargs):
        releasing.set()
        await finish_release.wait()
        await original_release(*args, **kwargs)

    monkeypatch.setattr(case.database, "release_unstarted_auto_slots", delayed_release)
    worker = TenantScheduler(
        SimpleNamespace(publish_random_topic=publish),
        SimpleNamespace(timezone="Europe/Moscow"),
    )
    task = asyncio.create_task(worker._publish_due_users("09:00", case.slot))
    created_tasks.append(task)
    try:
        await asyncio.wait_for(publishing.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(releasing.wait(), timeout=5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Повторная отмена прервала освобождение запусков"
    finally:
        finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    available = set(await case.database.claim_due_users("09:00", case.slot))
    assert available == {1001, 1002} - attempted
    assert not available.intersection(attempted)


@pytest.mark.asyncio
async def test_release_failure_is_logged_without_swallowing_cancellation(
    tenant_case, monkeypatch, created_tasks, caplog
):
    from tenant_scheduler import TenantScheduler

    case = tenant_case
    for user_id in (1001, 1002):
        await case.add_user(user_id)
    publishing = asyncio.Event()

    async def publish(user_id, *, trigger):
        publishing.set()
        await asyncio.Event().wait()

    failure = AsyncMock(side_effect=RuntimeError("synthetic release failure"))
    monkeypatch.setattr(case.database, "release_unstarted_auto_slots", failure)
    worker = TenantScheduler(
        SimpleNamespace(publish_random_topic=publish),
        SimpleNamespace(timezone="Europe/Moscow"),
    )
    task = asyncio.create_task(worker._publish_due_users("09:00", case.slot))
    created_tasks.append(task)
    await asyncio.wait_for(publishing.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    failure.assert_awaited_once()
    assert "synthetic release failure" in caplog.text
    assert await case.database.claim_due_users("09:00", case.slot) == []
