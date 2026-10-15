import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def qr(monkeypatch, background):
    import paid_multiuse

    bot = SimpleNamespace()
    monkeypatch.setattr(paid_multiuse, "require_paid", AsyncMock(return_value=True))
    monkeypatch.setattr(paid_multiuse, "_deps", lambda: (bot, None, None))
    monkeypatch.setattr(
        paid_multiuse.tenant_db,
        "tenant_dzen_account",
        AsyncMock(
            return_value={
                "comments_url": "https://dzen.ru/test",
                "profile_dir": "unused",
            }
        ),
    )
    monkeypatch.setattr(paid_multiuse.tenant_db, "set_tenant_dzen_enabled", AsyncMock())
    message = SimpleNamespace(
        text="test-login", from_user=SimpleNamespace(id=1001), answer=AsyncMock()
    )
    return paid_multiuse, message, SimpleNamespace(clear=AsyncMock())


@pytest.mark.asyncio
async def test_qr_authorization_participates_in_shutdown(
    qr, background, monkeypatch, created_tasks
):
    module, message, state = qr
    started, stopped = asyncio.Event(), asyncio.Event()

    async def authorize(**kwargs):
        created_tasks.append(asyncio.current_task())
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(module, "authorize_tenant_dzen_by_qr", authorize)
    await module.tenant_dzen_login_received(message, state)
    await asyncio.wait_for(started.wait(), 5)
    assert created_tasks[0] in background._BACKGROUND_TASKS
    assert created_tasks[0].get_name() == "tenant-dzen-auth-1001"
    await background.shutdown_background_tasks()
    assert stopped.is_set()
    assert created_tasks[0].cancelled()


@pytest.mark.asyncio
async def test_qr_authorization_does_not_start_after_shutdown(
    qr, background, monkeypatch
):
    module, message, state = qr
    authorize = AsyncMock()
    monkeypatch.setattr(module, "authorize_tenant_dzen_by_qr", authorize)
    await background.shutdown_background_tasks()
    with pytest.raises(RuntimeError, match="shutting down"):
        await module.tenant_dzen_login_received(message, state)
    authorize.assert_not_awaited()
