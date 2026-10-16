import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def admin_checkpoint_case(tmp_path, monkeypatch, created_tasks):
    import article_service
    import db

    path = tmp_path / "admin-publications.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_DB_LOCK", asyncio.Lock())
    await db.init_db(path)
    publication_id = await db.create_publication(
        topic_id=None,
        topic_title="Test publication topic",
        article_title="Test article title",
        article_body="Test full article body",
        image_path=None,
        trigger_type="test",
    )

    # Ignore any Dzen configuration inherited from the local environment.
    for name in ("DZEN_COMMENT_PROFILE_DIR", "DZEN_PROFILE_DIR", "DZEN_COMMENTS_URL"):
        monkeypatch.delenv(name, raising=False)

    formatter = SimpleNamespace(
        wait_for_article=AsyncMock(return_value=None),
        finalize_article=AsyncMock(return_value={"published": True}),
    )
    monkeypatch.setattr(
        article_service, "DzenRichFormatter", lambda **kwargs: formatter
    )
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=801))
    )
    service = object.__new__(article_service.ArticleService)
    service.cfg = SimpleNamespace(telegram_channel_id=-100201)
    service.bot = bot
    service.background_tasks = set()
    service.telegram_web = SimpleNamespace(close=AsyncMock())

    def enable_dzen():
        monkeypatch.setenv("DZEN_COMMENT_PROFILE_DIR", str(tmp_path))
        monkeypatch.setenv("DZEN_COMMENTS_URL", "synthetic studio")

    async def publish():
        try:
            return await service._publish_destinations(
                publication_id,
                "Test article title",
                "Test full article body",
                "Test short title",
                "Test short article body",
                b"synthetic image",
                None,
            )
        finally:
            # Own every background task, even when an assertion later fails.
            created_tasks.extend(service.background_tasks)

    async def read_publication():
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM publications WHERE id=?", (publication_id,)
            )
            row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    return SimpleNamespace(
        database=db,
        publication_id=publication_id,
        service=service,
        bot=bot,
        formatter=formatter,
        enable_dzen=enable_dzen,
        publish=publish,
        read_publication=read_publication,
    )


async def assert_saved_telegram_success(case):
    row = await case.read_publication()
    assert row["telegram_status"] == "published", (
        "Подтверждённый Telegram-пост потерял статус успеха"
    )
    assert row["telegram_message_id"] == "801", (
        "База потеряла номер уже отправленного Telegram-поста"
    )
    assert row["telegram_error"] is None


@pytest.mark.asyncio
async def test_admin_completed_publication_saves_message_id(admin_checkpoint_case):
    case = admin_checkpoint_case

    result = await case.publish()
    await case.service.close()

    assert result["telegram"] == "published"
    case.bot.send_photo.assert_awaited_once()
    await assert_saved_telegram_success(case)


@pytest.mark.asyncio
async def test_admin_success_is_saved_before_dzen_finishes(admin_checkpoint_case):
    case = admin_checkpoint_case
    case.enable_dzen()
    started = asyncio.Event()

    async def waiting_for_dzen(**kwargs):
        started.set()
        await asyncio.Event().wait()

    case.formatter.wait_for_article.side_effect = waiting_for_dzen
    result = await case.publish()
    await asyncio.wait_for(started.wait(), timeout=5)

    assert result["telegram"] == "published"
    assert case.service.background_tasks
    assert all(not task.done() for task in case.service.background_tasks)
    await assert_saved_telegram_success(case)


@pytest.mark.asyncio
async def test_admin_shutdown_during_dzen_keeps_message_id(admin_checkpoint_case):
    case = admin_checkpoint_case
    case.enable_dzen()
    started = asyncio.Event()

    async def waiting_for_dzen(**kwargs):
        started.set()
        await asyncio.Event().wait()

    case.formatter.wait_for_article.side_effect = waiting_for_dzen
    await case.publish()
    await asyncio.wait_for(started.wait(), timeout=5)
    tasks = tuple(case.service.background_tasks)
    await case.service.close()

    assert all(task.cancelled() for task in tasks)
    assert not case.service.background_tasks
    case.bot.send_photo.assert_awaited_once()
    case.formatter.finalize_article.assert_not_awaited()
    await assert_saved_telegram_success(case)


@pytest.mark.asyncio
async def test_admin_dzen_error_keeps_telegram_success(admin_checkpoint_case):
    case = admin_checkpoint_case
    case.enable_dzen()
    case.formatter.wait_for_article.side_effect = RuntimeError("synthetic Dzen failure")
    result = await case.publish()
    tasks = tuple(case.service.background_tasks)
    await asyncio.gather(*tasks)

    assert result["telegram"] == "published"
    case.bot.send_photo.assert_awaited_once()
    case.formatter.wait_for_article.assert_awaited_once()
    case.formatter.finalize_article.assert_not_awaited()
    await assert_saved_telegram_success(case)


@pytest.mark.asyncio
async def test_admin_telegram_failure_does_not_save_success(admin_checkpoint_case):
    case = admin_checkpoint_case
    case.bot.send_photo.side_effect = RuntimeError("synthetic Telegram failure")

    result = await case.publish()

    assert result["telegram"] == "error"
    case.bot.send_photo.assert_awaited_once()
    assert not case.service.background_tasks
    row = await case.read_publication()
    assert row["telegram_status"] == "error"
    assert row["telegram_message_id"] is None
    assert "synthetic Telegram failure" in row["telegram_error"]


@pytest.mark.asyncio
async def test_admin_cancel_during_checkpoint_keeps_confirmed_post(
    admin_checkpoint_case, created_tasks, monkeypatch
):
    case = admin_checkpoint_case
    started = asyncio.Event()
    allow_commit = asyncio.Event()
    save = case.database.set_telegram_result

    async def delayed_save(publication_id, status, **kwargs):
        if status == "published":
            started.set()
            await allow_commit.wait()
        await save(publication_id, status, **kwargs)

    monkeypatch.setattr(case.database, "set_telegram_result", delayed_save)
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    allow_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    case.bot.send_photo.assert_awaited_once()
    assert not case.service.background_tasks
    await assert_saved_telegram_success(case)


@pytest.mark.asyncio
async def test_admin_checkpoint_failure_is_reported_without_erasing_evidence(
    admin_checkpoint_case, monkeypatch
):
    case = admin_checkpoint_case
    save = case.database.set_telegram_result

    async def failing_save(publication_id, status, **kwargs):
        if status == "published":
            raise RuntimeError("synthetic checkpoint failure")
        await save(publication_id, status, **kwargs)

    writer = AsyncMock(side_effect=failing_save)
    monkeypatch.setattr(case.database, "set_telegram_result", writer)
    with pytest.raises(RuntimeError, match="synthetic checkpoint failure"):
        await case.publish()

    case.bot.send_photo.assert_awaited_once()
    assert not case.service.background_tasks
    assert writer.await_count == 1, "После подтверждения отправки записан ложный отказ"
    row = await case.read_publication()
    assert row["telegram_status"] == "pending"
    assert row["telegram_message_id"] is None


@pytest.mark.asyncio
async def test_admin_error_after_commit_keeps_saved_message_id(
    admin_checkpoint_case, monkeypatch
):
    case = admin_checkpoint_case
    save = case.database.set_telegram_result

    async def save_then_fail(publication_id, status, **kwargs):
        await save(publication_id, status, **kwargs)
        if status == "published":
            # Simulate a failure reported after SQLite has already committed.
            raise RuntimeError("synthetic post-commit failure")

    monkeypatch.setattr(case.database, "set_telegram_result", save_then_fail)
    try:
        await case.publish()
    except RuntimeError as exc:
        assert "synthetic post-commit failure" in str(exc)

    case.bot.send_photo.assert_awaited_once()
    assert not case.service.background_tasks
    await assert_saved_telegram_success(case)
