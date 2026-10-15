import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture(params=["admin", "tenant"])
async def publication_case(request, tmp_path, monkeypatch):
    import db
    import tenant_db
    from article_service import ArticleService
    from tenant_service import TenantArticleService

    path = tmp_path / "test.sqlite"
    topic_title = "Тестовая тема"
    user_id = 1001

    if request.param == "admin":
        database = db
        monkeypatch.setattr(database, "DB_PATH", path)
        monkeypatch.setattr(database, "_DB_LOCK", asyncio.Lock())
        await database.init_db(path)
        await database.seed_topics([topic_title])

        service = object.__new__(ArticleService)
        service.lock = asyncio.Lock()

        def publish():
            return service.publish_random_topic(trigger="urgent_random")

        async def reserve():
            return await database.reserve_random_topic()

        generation_method = "_generate_long_candidate"
        table = "topics"

    else:
        database = tenant_db
        monkeypatch.setattr(database, "DB_PATH", path)
        monkeypatch.setattr(database, "_DB_LOCK", asyncio.Lock())
        await database.init_db(path)
        await database.activate_subscription(
            user_id,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            source="test",
        )
        await database.connect_channel(
            user_id,
            chat_id=-100123,
            title="Тестовый канал",
            username=None,
        )
        await database.add_topics(user_id, [topic_title])

        service = object.__new__(TenantArticleService)
        service.cfg = SimpleNamespace(timezone="UTC")
        service._locks = {}

        def publish():
            return service.publish_random_topic(user_id, trigger="urgent_random")

        async def reserve():
            return await database.reserve_random_topic(user_id)

        generation_method = "_generate"
        table = "tenant_topics"

    return SimpleNamespace(
        path=path,
        kind=request.param,
        database=database,
        user_id=user_id,
        service=service,
        publish=publish,
        reserve=reserve,
        generation_method=generation_method,
        table=table,
        topic_title=topic_title,
    )


@pytest.mark.asyncio
async def test_cancel_before_publication_releases_topic(
    publication_case, created_tasks
):
    case = publication_case
    generation_started = asyncio.Event()

    async def fake_generation(*args, **kwargs):
        generation_started.set()
        await asyncio.Event().wait()

    setattr(case.service, case.generation_method, fake_generation)

    task = asyncio.create_task(case.publish())
    created_tasks.append(task)

    # Ждём, пока тема уже взята и подготовка статьи началась.
    await asyncio.wait_for(generation_started.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_topic_available(case)


async def read_topic_state(case):
    async with aiosqlite.connect(case.path) as connection:
        cursor = await connection.execute(
            f"SELECT reserved, active, used_at FROM {case.table}"
        )
        return await cursor.fetchone()


async def assert_topic_available(case):
    row = await read_topic_state(case)
    assert row is not None
    assert row[0] == 0, "После прерывания тема осталась занятой"
    assert row[1] == 1, "После прерывания тема стала неактивной"
    assert row[2] is None, "Неопубликованная тема отмечена использованной"

    next_topic = await case.reserve()
    assert next_topic is not None, "Тему нельзя снова взять в работу"
    assert next_topic["title"] == case.topic_title


def publish_automatically(case):
    if case.kind == "admin":
        return case.service.publish_random_topic(trigger="auto")
    return case.service.publish_random_topic(case.user_id, trigger="auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "error"])
async def test_interrupted_subtopic_selection_releases_topic(
    publication_case, created_tasks, interruption
):
    case = publication_case
    selection_started = asyncio.Event()

    async def selecting(*args, **kwargs):
        selection_started.set()
        if interruption == "error":
            raise RuntimeError("synthetic subtopic failure")
        await asyncio.Event().wait()

    case.service._select_auto_subtopic = selecting
    task = asyncio.create_task(publish_automatically(case))
    created_tasks.append(task)
    await asyncio.wait_for(selection_started.wait(), timeout=5)

    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="synthetic subtopic failure"):
            await task

    await assert_topic_available(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_case", ["admin"], indirect=True)
@pytest.mark.parametrize("interruption", ["cancel", "error"])
async def test_interrupted_short_and_image_generation_releases_topic(
    publication_case, created_tasks, interruption
):
    case = publication_case
    completion_started = asyncio.Event()
    case.service._generate_long_candidate = AsyncMock(
        return_value=("Test article title", "Test article body")
    )

    async def completing(*args, **kwargs):
        completion_started.set()
        if interruption == "error":
            raise RuntimeError("synthetic image failure")
        await asyncio.Event().wait()

    case.service._complete_generated_article = completing
    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await asyncio.wait_for(completion_started.wait(), timeout=5)

    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["status"] == "generation_error"

    await assert_topic_available(case)


@pytest.mark.asyncio
async def test_cancel_after_publication_starts_does_not_release_topic(
    publication_case, created_tasks, monkeypatch
):
    case = publication_case
    publication_started = asyncio.Event()
    # All image files belong to the temporary test directory.
    case.service.image_dir = case.path.parent
    release = AsyncMock(wraps=case.database.release_topic)
    monkeypatch.setattr(case.database, "release_topic", release)

    async def publishing(*args, **kwargs):
        publication_started.set()
        await asyncio.Event().wait()

    if case.kind == "admin":
        case.service._generate_long_candidate = AsyncMock(
            return_value=("Test article title", "Test article body")
        )
        case.service._complete_generated_article = AsyncMock(
            return_value=("Test short title", "Test short body", b"")
        )
        case.service._publish_destinations = publishing
    else:
        case.service._generate = AsyncMock(
            return_value=(
                "Test article title",
                "Test article body",
                "Test short body",
                b"synthetic-image",
            )
        )
        case.service._publish_channels = publishing

    task = asyncio.create_task(case.publish())
    created_tasks.append(task)
    await asyncio.wait_for(publication_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.assert_not_awaited()
    row = await read_topic_state(case)
    assert row is not None
    assert row[2] is not None, "Тема потеряла отметку начала публикации"
