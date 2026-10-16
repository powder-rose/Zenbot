import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def checkpoint_case(tmp_path, monkeypatch):
    import tenant_db
    import tenant_publish_pipeline
    from tenant_service import TenantArticleService

    path = tmp_path / "publications.sqlite"
    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    monkeypatch.setattr(tenant_db, "_DB_LOCK", asyncio.Lock())
    await tenant_db.init_db(path)

    user_id = 1001
    chat_ids = (-100101, -100102)
    await tenant_db.activate_subscription(
        user_id,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="test",
    )
    for chat_id in chat_ids:
        await tenant_db.connect_channel(
            user_id, chat_id=chat_id, title="Test channel", username=None
        )
    await tenant_db.add_topics(user_id, ["Test publication topic"])

    sent = []

    async def send(*, chat_id, **kwargs):
        message_id = 601 + len(sent)
        sent.append((chat_id, message_id))
        return SimpleNamespace(message_id=message_id)

    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=send),
        send_rich_message=AsyncMock(side_effect=send),
        send_message=AsyncMock(side_effect=send),
    )
    # Formatting is unrelated to persisting delivery; no real image is needed.
    monkeypatch.setattr(
        tenant_publish_pipeline,
        "build_short_rich_message",
        lambda *args, **kwargs: "synthetic rich message",
    )
    pipeline = tenant_publish_pipeline.TenantPublishPipeline(bot=bot)
    pipeline._get_dzen_config = AsyncMock(return_value=None)
    pipeline._publish_to_dzen = AsyncMock(
        return_value={"attempted": True, "synced": True, "error": None}
    )

    service = object.__new__(TenantArticleService)
    service.cfg = SimpleNamespace(timezone="UTC")
    service._locks = {}
    service.image_dir = tmp_path
    service.publish_pipeline = pipeline
    service._generate = AsyncMock(
        return_value=(
            "Test article title",
            "Test article body",
            "Test short body",
            b"synthetic image",
        )
    )

    def enable_dzen():
        pipeline._get_dzen_config.return_value = {
            "studio_url": "synthetic studio",
            "profile_dir": "unused",
        }

    def publish():
        return service.publish_random_topic(user_id, trigger="urgent_random")

    async def read_publication():
        # A separate SQLite connection verifies committed, persistent state.
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM tenant_publications WHERE user_id=?", (user_id,)
            )
            rows = await cursor.fetchall()
        assert len(rows) == 1
        return dict(rows[0])

    return SimpleNamespace(
        database=tenant_db,
        user_id=user_id,
        chat_ids=chat_ids,
        sent=sent,
        bot=bot,
        pipeline=pipeline,
        enable_dzen=enable_dzen,
        publish=publish,
        read_publication=read_publication,
    )


async def assert_saved_success(case, channel_count):
    row = await case.read_publication()
    assert row["channels_published"] == channel_count, (
        "База потеряла количество подтверждённых Telegram-публикаций"
    )
    assert row["status"] == "published", (
        "Отправленный Telegram-пост остался незавершённым в базе"
    )
    used_today = await case.database.successful_publications_today(case.user_id, "UTC")
    assert used_today == 1, "Отправленная статья не учитывается в суточном лимите"


async def cancel_after(task, started):
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("with_dzen", [False, True], ids=["telegram", "dzen"])
async def test_completed_publication_keeps_success(checkpoint_case, with_dzen):
    case = checkpoint_case
    if with_dzen:
        case.enable_dzen()

    result = await case.publish()

    assert result["status"] == "ok"
    assert result["channels_published"] == 2
    assert [chat_id for chat_id, _ in case.sent] == list(case.chat_ids)
    await assert_saved_success(case, 2)


@pytest.mark.asyncio
async def test_dzen_error_does_not_erase_telegram_success(checkpoint_case):
    case = checkpoint_case
    case.enable_dzen()
    case.pipeline._publish_to_dzen.return_value = {
        "attempted": True,
        "synced": False,
        "error": "synthetic Dzen failure",
    }

    result = await case.publish()

    assert result["status"] == "ok"
    assert len(case.sent) == 2
    await assert_saved_success(case, 2)


@pytest.mark.asyncio
async def test_cancel_during_first_dzen_keeps_telegram_success(
    checkpoint_case, created_tasks
):
    case = checkpoint_case
    case.enable_dzen()
    started = asyncio.Event()

    async def waiting_for_dzen(**kwargs):
        started.set()
        await asyncio.Event().wait()

    case.pipeline._publish_to_dzen.side_effect = waiting_for_dzen
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await cancel_after(task, started)

    assert case.sent == [(case.chat_ids[0], 601)]
    await assert_saved_success(case, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_dzen", [False, True], ids=["telegram", "dzen"])
async def test_cancel_sending_second_channel_keeps_first_success(
    checkpoint_case, created_tasks, with_dzen
):
    case = checkpoint_case
    if with_dzen:
        case.enable_dzen()
    started = asyncio.Event()

    async def sending(*, chat_id, **kwargs):
        if chat_id == case.chat_ids[1]:
            started.set()
            await asyncio.Event().wait()
        case.sent.append((chat_id, 601))
        return SimpleNamespace(message_id=601)

    case.bot.send_photo.side_effect = sending
    case.bot.send_rich_message.side_effect = sending
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await cancel_after(task, started)

    assert case.sent == [(case.chat_ids[0], 601)]
    await assert_saved_success(case, 1)


@pytest.mark.asyncio
async def test_cancel_during_second_dzen_keeps_both_telegram_successes(
    checkpoint_case, created_tasks
):
    case = checkpoint_case
    case.enable_dzen()
    started = asyncio.Event()

    async def sync_dzen(*, chat_id, **kwargs):
        if chat_id == case.chat_ids[1]:
            started.set()
            await asyncio.Event().wait()
        return {"attempted": True, "synced": True, "error": None}

    case.pipeline._publish_to_dzen.side_effect = sync_dzen
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await cancel_after(task, started)

    assert [chat_id for chat_id, _ in case.sent] == list(case.chat_ids)
    await assert_saved_success(case, 2)


@pytest.mark.asyncio
async def test_success_is_committed_before_dzen_finishes(
    checkpoint_case, created_tasks
):
    case = checkpoint_case
    case.enable_dzen()
    started = asyncio.Event()

    async def waiting_for_dzen(**kwargs):
        started.set()
        await asyncio.Event().wait()

    case.pipeline._publish_to_dzen.side_effect = waiting_for_dzen
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        # Inspect the DB while Dzen is still running, before cancellation.
        assert not task.done()
        await assert_saved_success(case, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_checkpoint_waits_for_committed_success(
    checkpoint_case, created_tasks, monkeypatch
):
    case = checkpoint_case
    case.enable_dzen()
    started = asyncio.Event()
    allow_commit = asyncio.Event()
    finish = case.database.finish_publication

    async def delayed_finish(*args, **kwargs):
        if kwargs["channels_published"] == 1:
            started.set()
            await allow_commit.wait()
        await finish(*args, **kwargs)

    monkeypatch.setattr(case.database, "finish_publication", delayed_finish)
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    allow_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert case.sent == [(case.chat_ids[0], 601)]
    case.pipeline._publish_to_dzen.assert_not_awaited()
    await assert_saved_success(case, 1)


@pytest.mark.asyncio
async def test_checkpoint_failure_stops_further_sends(checkpoint_case, monkeypatch):
    case = checkpoint_case
    case.enable_dzen()
    monkeypatch.setattr(
        case.database,
        "finish_publication",
        AsyncMock(side_effect=RuntimeError("synthetic checkpoint failure")),
    )

    with pytest.raises(RuntimeError, match="synthetic checkpoint failure"):
        await case.publish()

    assert case.sent == [(case.chat_ids[0], 601)]
    case.pipeline._publish_to_dzen.assert_not_awaited()
    row = await case.read_publication()
    assert row["channels_published"] == 0
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_unhandled_dzen_error_keeps_confirmed_telegram_success(checkpoint_case):
    case = checkpoint_case
    case.enable_dzen()
    case.pipeline._publish_to_dzen.side_effect = RuntimeError(
        "synthetic unhandled Dzen failure"
    )

    result = await case.publish()

    assert result["status"] == "ok"
    assert result["channels_published"] == 2
    assert [chat_id for chat_id, _ in case.sent] == list(case.chat_ids)
    await assert_saved_success(case, 2)
    row = await case.read_publication()
    assert "synthetic unhandled Dzen failure" in row["error"]


@pytest.mark.asyncio
async def test_failed_telegram_send_does_not_count_as_published(checkpoint_case):
    case = checkpoint_case
    case.bot.send_rich_message.side_effect = RuntimeError("synthetic send failure")
    case.bot.send_photo.side_effect = RuntimeError("synthetic send failure")

    result = await case.publish()

    assert result["status"] == "publish_error"
    assert result["channels_published"] == 0
    assert case.sent == []
    row = await case.read_publication()
    assert row["status"] == "error"
    assert row["channels_published"] == 0
    assert "synthetic send failure" in row["error"]
    assert await case.database.successful_publications_today(case.user_id, "UTC") == 0
