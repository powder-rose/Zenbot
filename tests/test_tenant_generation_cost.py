import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def generation_case(tmp_path, monkeypatch):
    import tenant_db
    from tenant_service import TenantArticleService

    path = tmp_path / "generation-cost.sqlite"
    user_id = 1001
    topic_title = "Тестовая тема"
    previous_title = "Проверка огнетушителей на предприятии"
    accepted_title = "Выращивание орхидей в домашних условиях"
    full_body = "Synthetic full article body"
    short_body = "Synthetic short article body"
    image_bytes = b"synthetic-image"

    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    monkeypatch.setattr(tenant_db, "_DB_LOCK", asyncio.Lock())
    await tenant_db.init_db(path)
    await tenant_db.activate_subscription(
        user_id,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="test",
    )
    await tenant_db.connect_channel(
        user_id, chat_id=-100123, title="Test channel", username=None
    )
    await tenant_db.add_topics(user_id, [topic_title])
    history_id = await tenant_db.create_publication(
        user_id=user_id,
        topic_id=None,
        topic_title="Previous topic",
        article_title=previous_title,
        article_body="Previous published article",
        image_path=None,
        trigger_type="test",
    )
    await tenant_db.finish_publication(
        history_id, status="published", channels_published=1
    )

    service = object.__new__(TenantArticleService)
    service.cfg = SimpleNamespace(timezone="UTC")
    service._locks = {}
    service.image_dir = tmp_path / "images"
    service.image_dir.mkdir()
    service.search = SimpleNamespace(
        search=AsyncMock(
            return_value=[
                {
                    "title": "Synthetic source",
                    "snippet": "Synthetic source text",
                    "url": "https://offline.invalid/source",
                }
            ]
        )
    )
    service.gpt = SimpleNamespace(
        generate_article_from_sources=AsyncMock(
            return_value=(accepted_title, full_body)
        ),
        generate_syncbot_article_from_article=AsyncMock(
            return_value=("Unused short title", short_body)
        ),
    )
    service.art = SimpleNamespace(generate_image=AsyncMock(return_value=image_bytes))
    # Exercise real generation and database logic, without sending a real post.
    service._publish_channels = AsyncMock(return_value=(1, []))

    def publish():
        # Avoid automatic topic retries and urgent one-time topic deactivation.
        return service.publish_random_topic(user_id, trigger="test")

    async def read_new_publications():
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM tenant_publications WHERE user_id=? AND id!=?",
                (user_id, history_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def assert_topic_available(*, deferred=False):
        async with aiosqlite.connect(path) as connection:
            cursor = await connection.execute(
                "SELECT reserved, active, used_at FROM tenant_topics WHERE user_id=?",
                (user_id,),
            )
            state = await cursor.fetchone()
        assert state is not None
        assert tuple(state[:2]) == (0, 1), "Тема заблокирована или стала неактивной"
        if deferred:
            # A similar article moves the topic to the back of the queue.
            # used_at is a queue timestamp, not evidence of a published post.
            assert state[2] is not None, "Похожая тема не отложена в конец очереди"
        else:
            assert state[2] is None, "Ошибка генерации изменила очередь тем"
        assert await tenant_db.reserve_random_topic(user_id) is not None

    return SimpleNamespace(
        database=tenant_db,
        user_id=user_id,
        topic_title=topic_title,
        previous_title=previous_title,
        accepted_title=accepted_title,
        full_body=full_body,
        short_body=short_body,
        image_bytes=image_bytes,
        service=service,
        publish=publish,
        read_new_publications=read_new_publications,
        assert_topic_available=assert_topic_available,
    )


async def assert_duplicate_stopped_before_extra_generation(case, result):
    assert result["status"] == "similar_article_skipped"
    assert result["similar_to"] == case.previous_title
    assert 0.58 <= result["similarity"] <= 1
    case.service.gpt.generate_syncbot_article_from_article.assert_not_awaited()
    case.service.art.generate_image.assert_not_awaited()
    case.service._publish_channels.assert_not_awaited()
    assert await case.read_new_publications() == []
    assert list(case.service.image_dir.iterdir()) == []
    await case.assert_topic_available(deferred=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_title",
    [
        "Проверка огнетушителей на предприятии",
        "ПРОВЕРКА ОГНЕТУШИТЕЛЕЙ НА ПРЕДПРИЯТИИ!",
        "Проверка огнетушителей на предприятии: инструкция",
    ],
    ids=["exact", "case-and-punctuation", "similar-title"],
)
async def test_rejected_title_does_not_generate_short_or_image(
    generation_case, candidate_title
):
    case = generation_case
    case.service.gpt.generate_article_from_sources.return_value = (
        candidate_title,
        case.full_body,
    )

    result = await case.publish()

    case.service.search.search.assert_awaited_once()
    case.service.gpt.generate_article_from_sources.assert_awaited_once()
    await assert_duplicate_stopped_before_extra_generation(case, result)


@pytest.mark.asyncio
async def test_rejected_title_after_freshness_retry_skips_short_and_image(
    generation_case,
):
    case = generation_case
    case.service.gpt.generate_article_from_sources.side_effect = [
        RuntimeError("устаревшая новостная подача"),
        (case.previous_title, case.full_body),
    ]

    result = await case.publish()

    assert case.service.search.search.await_count == 2
    assert case.service.gpt.generate_article_from_sources.await_count == 2
    await assert_duplicate_stopped_before_extra_generation(case, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("freshness_retry", [False, True], ids=["first", "retry"])
async def test_accepted_title_completes_generation_with_custom_prompts(
    generation_case, freshness_retry
):
    case = generation_case
    for key, value in (
        ("prompt_article_system", "Custom LONG instructions"),
        ("prompt_short_system", "Custom SHORT instructions"),
        ("prompt_image_template", "Custom image for {topic}"),
    ):
        await case.database.set_setting(case.user_id, key, value)
    if freshness_retry:
        case.service.gpt.generate_article_from_sources.side_effect = [
            RuntimeError("устаревшая новостная подача"),
            (case.accepted_title, case.full_body),
        ]

    result = await case.publish()

    assert result["status"] == "ok"
    assert result["article_title"] == case.accepted_title
    assert case.service.search.search.await_count == 1 + int(freshness_retry)
    long_calls = case.service.gpt.generate_article_from_sources.await_args_list
    assert len(long_calls) == 1 + int(freshness_retry)
    assert all(
        call.kwargs["system_prompt"] == "Custom LONG instructions"
        for call in long_calls
    )
    case.service.gpt.generate_syncbot_article_from_article.assert_awaited_once_with(
        topic=case.topic_title,
        article_title=case.accepted_title,
        article_body=case.full_body,
        max_chars=820,
        system_prompt="Custom SHORT instructions",
    )
    case.service.art.generate_image.assert_awaited_once_with(
        f"Custom image for {case.topic_title}"
    )
    rows = await case.read_new_publications()
    assert len(rows) == 1
    assert rows[0]["id"] == result["publication_id"]
    assert rows[0]["status"] == "published"
    assert rows[0]["channels_published"] == 1
    assert Path(rows[0]["image_path"]).read_bytes() == case.image_bytes
    case.service._publish_channels.assert_awaited_once_with(
        case.user_id,
        case.accepted_title,
        case.full_body,
        case.short_body,
        case.image_bytes,
        publication_id=result["publication_id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["long", "short", "image"])
async def test_generation_failure_stops_later_paid_calls_and_releases_topic(
    generation_case, failure_stage
):
    case = generation_case
    calls = [
        case.service.gpt.generate_article_from_sources,
        case.service.gpt.generate_syncbot_article_from_article,
        case.service.art.generate_image,
    ]
    failed_index = ["long", "short", "image"].index(failure_stage)
    calls[failed_index].side_effect = RuntimeError("synthetic generation failure")

    result = await case.publish()

    assert result["status"] == "generation_error"
    assert "synthetic generation failure" in result["error"]
    for call in calls[: failed_index + 1]:
        call.assert_awaited_once()
    for call in calls[failed_index + 1 :]:
        call.assert_not_awaited()
    case.service._publish_channels.assert_not_awaited()
    assert await case.read_new_publications() == []
    assert list(case.service.image_dir.iterdir()) == []
    await case.assert_topic_available()
