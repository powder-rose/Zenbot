import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def integrity_case(tmp_path, monkeypatch):
    import tenant_db

    path = tmp_path / "tenant-integrity.sqlite"
    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    monkeypatch.setattr(tenant_db, "_DB_LOCK", asyncio.Lock())
    await tenant_db.init_db(path)

    async def create_publication(user_id):
        return await tenant_db.create_publication(
            user_id=user_id,
            topic_id=None,
            topic_title="Test topic",
            article_title="Test article",
            article_body="Test article body",
            image_path=None,
            trigger_type="test",
        )

    async def rows(table):
        allowed = {
            "app_users",
            "subscriptions",
            "tenant_channels",
            "tenant_topics",
            "tenant_schedule",
            "tenant_settings",
            "tenant_publications",
        }
        assert table in allowed
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(f"SELECT * FROM {table}")
            return [dict(row) for row in await cursor.fetchall()]

    async def violations():
        async with aiosqlite.connect(path) as connection:
            cursor = await connection.execute("PRAGMA foreign_key_check")
            return await cursor.fetchall()

    async def register(user_id=1001):
        await tenant_db.touch_user(
            SimpleNamespace(
                id=user_id,
                username="test-user",
                first_name="Test",
                last_name=None,
                language_code="ru",
            )
        )

    return SimpleNamespace(
        database=tenant_db,
        path=path,
        create_publication=create_publication,
        rows=rows,
        violations=violations,
        register=register,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reinitialize", [False, True], ids=["first-start", "restart"])
async def test_publication_cannot_reference_missing_user(integrity_case, reinitialize):
    case = integrity_case
    if reinitialize:
        await case.database.init_db(case.path)

    with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY"):
        await case.create_publication(404404)

    assert await case.rows("tenant_publications") == []
    assert await case.rows("app_users") == []
    assert await case.violations() == []


@pytest.mark.asyncio
async def test_registered_user_can_create_publication(integrity_case):
    case = integrity_case
    await case.register()

    publication_id = await case.create_publication(1001)

    rows = await case.rows("tenant_publications")
    assert len(rows) == 1
    assert rows[0]["id"] == publication_id
    assert rows[0]["user_id"] == 1001
    assert rows[0]["status"] == "pending"
    assert await case.violations() == []


async def populate_valid_user(case):
    # Existing helpers intentionally register a new user when needed.
    await case.database.activate_subscription(
        1001,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="test",
    )
    connected = await case.database.connect_channel(
        1001, chat_id=-100301, title="Test channel", username=None
    )
    assert connected == "ok"
    await case.database.ensure_defaults(
        1001, publish_times=["12:00"], topics=["Test topic"]
    )
    return await case.create_publication(1001)


@pytest.mark.asyncio
async def test_user_setup_keeps_valid_relationships(integrity_case):
    case = integrity_case
    await populate_valid_user(case)

    for table in (
        "app_users",
        "subscriptions",
        "tenant_channels",
        "tenant_topics",
        "tenant_schedule",
        "tenant_publications",
    ):
        rows = await case.rows(table)
        assert len(rows) == 1, table
        assert rows[0]["user_id"] == 1001, table
    assert await case.violations() == []


@pytest.mark.asyncio
async def test_reinitialization_preserves_valid_data(integrity_case):
    case = integrity_case
    publication_id = await populate_valid_user(case)
    await case.database.finish_publication(
        publication_id, status="published", channels_published=1
    )
    tables = (
        "app_users",
        "subscriptions",
        "tenant_channels",
        "tenant_topics",
        "tenant_schedule",
        "tenant_settings",
        "tenant_publications",
    )
    before = {table: await case.rows(table) for table in tables}

    await case.database.init_db(case.path)

    after = {table: await case.rows(table) for table in tables}
    assert after == before, "Повторный запуск изменил существующие данные"
    assert await case.violations() == []


@pytest.mark.asyncio
async def test_reinitialization_does_not_delete_legacy_orphan(integrity_case):
    case = integrity_case
    # Simulate an old inconsistent database, only in the temporary directory.
    async with aiosqlite.connect(case.path) as connection:
        await connection.execute("PRAGMA foreign_keys=OFF")
        await connection.execute(
            """
            INSERT INTO tenant_publications(
                user_id, topic_title, article_title, article_body,
                trigger_type, status, created_at
            ) VALUES(404404, 'Legacy topic', 'Legacy article', 'Saved body',
                     'test', 'published', '2026-01-01T00:00:00+00:00')
            """
        )
        await connection.commit()
    before = await case.rows("tenant_publications")
    problems_before = await case.violations()
    assert len(problems_before) == 1
    assert problems_before[0][0] == "tenant_publications"

    await case.database.init_db(case.path)

    assert await case.rows("tenant_publications") == before, (
        "Повторный запуск удалил или исправил старые данные без согласования"
    )
    assert await case.rows("app_users") == []
    assert await case.violations() == problems_before


@pytest.mark.asyncio
async def test_rejected_publication_does_not_block_later_valid_write(integrity_case):
    case = integrity_case
    user_id = 404404
    with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY"):
        await case.create_publication(user_id)
    assert await case.rows("tenant_publications") == []

    await case.register(user_id)
    publication_id = await case.create_publication(user_id)

    rows = await case.rows("tenant_publications")
    assert len(rows) == 1
    assert rows[0]["id"] == publication_id
    assert rows[0]["user_id"] == user_id
    assert rows[0]["status"] == "pending"
    assert await case.violations() == []
