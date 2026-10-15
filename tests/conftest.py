import asyncio
import socket

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def offline_tests(monkeypatch):
    """Tests must not read production settings or connect to external services."""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise AssertionError("External network access is forbidden in tests")

    def guarded_connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect_ex(sock, address)
        raise AssertionError("External network access is forbidden in tests")

    # Windows uses loopback sockets internally to create an asyncio event loop.
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest_asyncio.fixture
async def created_tasks():
    tasks = []
    yield tasks
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def background(monkeypatch):
    import telegram_safe

    monkeypatch.setattr(telegram_safe, "_BACKGROUND_TASKS", set())
    monkeypatch.setattr(
        telegram_safe, "_BACKGROUND_TASKS_CLOSING", False, raising=False
    )
    return telegram_safe
