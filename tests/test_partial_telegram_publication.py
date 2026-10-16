import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage, SendPhoto


@pytest_asyncio.fixture
async def partial_case(tmp_path, monkeypatch):
    import tenant_db
    import tenant_publish_pipeline
    from tenant_service import TenantArticleService

    path = tmp_path / "partial-publication.sqlite"
    user_id = 1001
    chat_id = -100811
    second_chat_id = -100812
    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    monkeypatch.setattr(tenant_db, "_DB_LOCK", asyncio.Lock())
    await tenant_db.init_db(path)
    await tenant_db.activate_subscription(
        user_id,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="test",
    )
    await tenant_db.connect_channel(
        user_id, chat_id=chat_id, title="Test channel", username=None
    )
    await tenant_db.add_topics(user_id, ["Partial publication topic"])
    accepted = []

    def record(kind, destination):
        message_id = 870001 + len(accepted)
        accepted.append((kind, destination, message_id))
        return SimpleNamespace(message_id=message_id)

    async def photo(*, chat_id, **kwargs):
        return record("photo", chat_id)

    async def text(*, chat_id, **kwargs):
        return record("text", chat_id)

    # No Rich Message method: exercise the real photo + text fallback.
    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=photo),
        send_message=AsyncMock(side_effect=text),
        delete_message=AsyncMock(),
    )
    monkeypatch.setattr(
        tenant_publish_pipeline,
        "build_short_rich_message",
        lambda *args, **kwargs: "synthetic rich message",
    )
    pipeline = tenant_publish_pipeline.TenantPublishPipeline(bot=bot)
    pipeline._get_dzen_config = AsyncMock(return_value=None)
    pipeline._publish_to_dzen = AsyncMock()
    service = object.__new__(TenantArticleService)
    service.cfg = SimpleNamespace(timezone="UTC")
    service._locks = {}
    service.image_dir = tmp_path
    service.publish_pipeline = pipeline
    service._generate = AsyncMock()

    def set_short(body):
        service._generate.return_value = (
            "Partial publication article",
            "Long article body",
            body,
            b"synthetic image",
        )

    set_short("Short article body")

    def publish():
        return service.publish_random_topic(user_id, trigger="urgent_random")

    async def read_publication():
        # Read committed state through a separate connection.
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM tenant_publications WHERE user_id=?", (user_id,)
            )
            rows = await cursor.fetchall()
        assert len(rows) == 1
        return dict(rows[0])

    async def add_second_channel():
        await tenant_db.connect_channel(
            user_id, chat_id=second_chat_id, title="Second test channel", username=None
        )

    return SimpleNamespace(
        database=tenant_db,
        user_id=user_id,
        chat_id=chat_id,
        second_chat_id=second_chat_id,
        bot=bot,
        pipeline=pipeline,
        accepted=accepted,
        record=record,
        set_short=set_short,
        publish=publish,
        read_publication=read_publication,
        add_second_channel=add_second_channel,
    )


async def assert_partial_evidence(case, chat_id, message_ids, *, complete_channels=0):
    row = await case.read_publication()
    assert row["channels_published"] == complete_channels, (
        "Частично отправленный канал ошибочно посчитан полностью опубликованным"
    )
    if complete_channels:
        assert row["status"] == "published", "Потерян успех предыдущего канала"
    else:
        assert row["status"] != "published"
    evidence = row["error"] or ""
    assert str(chat_id) in evidence, "Не сохранён канал частичной отправки"
    for message_id in message_ids:
        assert str(message_id) in evidence, (
            f"Не сохранён номер подтверждённого Telegram-сообщения {message_id}"
        )
    case.bot.delete_message.assert_not_awaited()


async def cancel_at(task, entered):
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_complete_fallback_counts_one_published_channel(partial_case):
    case = partial_case
    result = await case.publish()

    assert result["status"] == "ok"
    assert result["channels_published"] == 1
    assert [item[0] for item in case.accepted] == ["photo", "text"]
    row = await case.read_publication()
    assert row["status"] == "published"
    assert row["channels_published"] == 1
    assert await case.database.successful_publications_today(case.user_id, "UTC") == 1
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_photo_without_confirmation_does_not_start_text(partial_case):
    case = partial_case
    case.bot.send_photo.side_effect = RuntimeError("synthetic photo failure")

    result = await case.publish()

    assert result["status"] == "publish_error"
    assert result["channels_published"] == 0
    assert case.accepted == []
    assert (await case.read_publication())["channels_published"] == 0
    case.bot.send_photo.assert_awaited_once()
    case.bot.send_message.assert_not_awaited()
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_photo_reply_does_not_send_photo_again_or_start_text(partial_case):
    case = partial_case
    failure = TelegramNetworkError(
        method=SendPhoto(chat_id=case.chat_id, photo="synthetic-file-id"),
        message="synthetic photo reply lost",
    )

    async def accepted_without_reply(*, chat_id, **kwargs):
        # Physical acceptance in this fake does not give the caller an ID.
        case.record("photo", chat_id)
        raise failure

    case.bot.send_photo.side_effect = accepted_without_reply
    result = await case.publish()

    assert result["status"] == "publish_error"
    assert len(case.accepted) == 1
    assert (await case.read_publication())["channels_published"] == 0
    case.bot.send_photo.assert_awaited_once()
    case.bot.send_message.assert_not_awaited()
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_text_does_not_repeat_or_delete_confirmed_photo(partial_case):
    case = partial_case
    case.bot.send_message.side_effect = RuntimeError("synthetic text failure")

    result = await case.publish()

    assert result["status"] == "publish_error"
    assert result["channels_published"] == 0
    assert [item[0] for item in case.accepted] == ["photo"]
    case.bot.send_photo.assert_awaited_once()
    case.bot.send_message.assert_awaited_once()
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed_count", [1, 2], ids=["photo", "text-chunk"])
async def test_partial_checkpoint_failure_stops_further_sends_and_logs_ids(
    partial_case, monkeypatch, caplog, confirmed_count
):
    case = partial_case
    await case.add_second_channel()
    case.set_short("A" * 8100)
    original_finish = case.database.finish_publication

    async def failing_finish(*args, **kwargs):
        if len(case.accepted) == confirmed_count:
            raise RuntimeError("synthetic partial checkpoint failure")
        await original_finish(*args, **kwargs)

    monkeypatch.setattr(case.database, "finish_publication", failing_finish)
    with pytest.raises(RuntimeError, match="synthetic partial checkpoint failure"):
        await case.publish()

    assert len(case.accepted) == confirmed_count
    assert all(item[1] == case.chat_id for item in case.accepted)
    case.bot.send_photo.assert_awaited_once()
    assert case.bot.send_message.await_count == confirmed_count - 1
    assert str(case.chat_id) in caplog.text
    for _, _, message_id in case.accepted:
        assert str(message_id) in caplog.text
    row = await case.read_publication()
    assert row["status"] != "published"
    assert row["channels_published"] == 0
    if confirmed_count == 2:
        # The previous committed photo must survive a later storage failure.
        await assert_partial_evidence(case, case.chat_id, [case.accepted[0][2]])
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("at_complete", [False, True], ids=["partial", "complete"])
async def test_repeated_cancellation_waits_for_fragment_checkpoint(
    partial_case, monkeypatch, created_tasks, at_complete
):
    case = partial_case
    entered = asyncio.Event()
    allow_commit = asyncio.Event()
    original_finish = case.database.finish_publication

    async def delayed_finish(*args, **kwargs):
        if (kwargs["channels_published"] > 0) == at_complete:
            entered.set()
            await allow_commit.wait()
        await original_finish(*args, **kwargs)

    monkeypatch.setattr(case.database, "finish_publication", delayed_finish)
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Отмена прервала сохранение подтверждений Telegram"
    finally:
        allow_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    row = await case.read_publication()
    if at_complete:
        assert [item[0] for item in case.accepted] == ["photo", "text"]
        assert row["status"] == "published"
        assert row["channels_published"] == 1
        assert row["error"] is None
    else:
        assert [item[0] for item in case.accepted] == ["photo"]
        await assert_partial_evidence(case, case.chat_id, [case.accepted[0][2]])
    case.bot.send_photo.assert_awaited_once()
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkpoint_failure_during_cancellation_preserves_signal_and_logs_ids(
    partial_case, monkeypatch, created_tasks, caplog
):
    case = partial_case
    entered = asyncio.Event()
    allow_failure = asyncio.Event()

    async def failing_finish(*args, **kwargs):
        entered.set()
        await allow_failure.wait()
        raise RuntimeError("synthetic cancelled partial checkpoint failure")

    monkeypatch.setattr(case.database, "finish_publication", failing_finish)
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        allow_failure.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert "synthetic cancelled partial checkpoint failure" in caplog.text
    assert str(case.accepted[0][2]) in caplog.text
    case.bot.send_photo.assert_awaited_once()
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_complete_channel_does_not_erase_earlier_partial_evidence(
    partial_case,
):
    case = partial_case
    await case.add_second_channel()

    async def send_text(*, chat_id, **kwargs):
        if chat_id == case.chat_id:
            raise RuntimeError("synthetic first channel text failure")
        return case.record("text", chat_id)

    case.bot.send_message.side_effect = send_text
    result = await case.publish()

    assert result["status"] == "ok"
    assert result["channels_published"] == 1
    assert [item[0] for item in case.accepted] == ["photo", "photo", "text"]
    await assert_partial_evidence(
        case, case.chat_id, [case.accepted[0][2]], complete_channels=1
    )


@pytest.mark.asyncio
async def test_photo_without_text_is_complete_and_not_counted_twice(partial_case):
    case = partial_case
    case.set_short("")

    result = await case.publish()

    assert result["status"] == "ok"
    assert result["channels_published"] == 1
    assert [item[0] for item in case.accepted] == ["photo"]
    row = await case.read_publication()
    assert row["status"] == "published"
    assert row["channels_published"] == 1
    assert row["error"] is None
    case.bot.send_message.assert_not_awaited()
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_chunked_fallback_counts_channel_not_messages(partial_case):
    case = partial_case
    case.set_short("A" * 8100)

    result = await case.publish()

    assert result["status"] == "ok"
    assert result["channels_published"] == 1
    assert [item[0] for item in case.accepted] == ["photo", "text", "text", "text"]
    assert (await case.read_publication())["channels_published"] == 1
    assert await case.database.successful_publications_today(case.user_id, "UTC") == 1
    case.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_failure_keeps_confirmed_photo_evidence(partial_case):
    case = partial_case
    case.bot.send_message.side_effect = RuntimeError("synthetic text failure")

    result = await case.publish()

    assert result["status"] == "publish_error"
    assert [item[0] for item in case.accepted] == ["photo"]
    await assert_partial_evidence(case, case.chat_id, [case.accepted[0][2]])


@pytest.mark.asyncio
async def test_cancellation_during_text_keeps_confirmed_photo_evidence(
    partial_case, created_tasks
):
    case = partial_case
    entered = asyncio.Event()

    async def waiting_text(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    case.bot.send_message.side_effect = waiting_text
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await cancel_at(task, entered)

    assert [item[0] for item in case.accepted] == ["photo"]
    await assert_partial_evidence(case, case.chat_id, [case.accepted[0][2]])


@pytest.mark.asyncio
async def test_later_chunk_failure_keeps_all_confirmed_message_ids(partial_case):
    case = partial_case
    case.set_short("A" * 8100)

    async def send_until_second_chunk(*, chat_id, **kwargs):
        if case.bot.send_message.await_count == 2:
            raise RuntimeError("synthetic second text chunk failure")
        return case.record("text", chat_id)

    case.bot.send_message.side_effect = send_until_second_chunk
    result = await case.publish()

    assert result["status"] == "publish_error"
    assert [item[0] for item in case.accepted] == ["photo", "text"]
    await assert_partial_evidence(
        case, case.chat_id, [item[2] for item in case.accepted]
    )


@pytest.mark.asyncio
async def test_photo_evidence_is_committed_while_text_is_still_waiting(
    partial_case, created_tasks
):
    case = partial_case
    entered = asyncio.Event()

    async def waiting_text(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    case.bot.send_message.side_effect = waiting_text
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done()
        await assert_partial_evidence(case, case.chat_id, [case.accepted[0][2]])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["error", "cancel"])
async def test_partial_second_channel_preserves_first_success_and_its_own_evidence(
    partial_case, created_tasks, interruption
):
    case = partial_case
    await case.add_second_channel()
    entered = asyncio.Event()

    async def send_text(*, chat_id, **kwargs):
        if chat_id == case.second_chat_id:
            if interruption == "error":
                raise RuntimeError("synthetic second channel text failure")
            entered.set()
            await asyncio.Event().wait()
        return case.record("text", chat_id)

    case.bot.send_message.side_effect = send_text
    if interruption == "error":
        result = await case.publish()
        assert result["channels_published"] == 1
    else:
        task = asyncio.create_task(case.publish())
        created_tasks.append(task)
        await cancel_at(task, entered)

    assert [item[0] for item in case.accepted] == ["photo", "text", "photo"]
    await assert_partial_evidence(
        case, case.second_chat_id, [case.accepted[-1][2]], complete_channels=1
    )
    assert await case.database.successful_publications_today(case.user_id, "UTC") == 1


@pytest.mark.asyncio
async def test_lost_text_reply_preserves_only_confirmed_message_evidence(partial_case):
    case = partial_case
    failure = TelegramNetworkError(
        method=SendMessage(chat_id=case.chat_id, text="synthetic text"),
        message="synthetic text reply lost",
    )

    async def accepted_without_reply(*, chat_id, **kwargs):
        case.record("text", chat_id)
        raise failure

    case.bot.send_message.side_effect = accepted_without_reply
    result = await case.publish()

    assert result["status"] == "publish_error"
    assert [item[0] for item in case.accepted] == ["photo", "text"]
    row = await case.read_publication()
    assert str(case.accepted[1][2]) not in (row["error"] or ""), (
        "Номер сообщения с потерянным ответом ошибочно считается подтверждённым"
    )
    await assert_partial_evidence(case, case.chat_id, [case.accepted[0][2]])
    case.bot.send_photo.assert_awaited_once()
    case.bot.send_message.assert_awaited_once()
