import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendRichMessage
from aiogram.types import InputRichMessage


@pytest.fixture
def fallback_case(monkeypatch):
    import tenant_publish_pipeline

    chat_id = -100401
    short_body = "Test short article body"
    rich = InputRichMessage(html="<b>Test article</b>")
    monkeypatch.setattr(
        tenant_publish_pipeline,
        "build_short_rich_message",
        lambda *args, **kwargs: rich,
    )
    accepted = []

    async def send_rich(**kwargs):
        accepted.append(("rich", 801))
        return SimpleNamespace(message_id=801)

    async def send_photo(**kwargs):
        accepted.append(("photo", 802))
        return SimpleNamespace(message_id=802)

    async def send_text(**kwargs):
        accepted.append(("text", 803))
        return SimpleNamespace(message_id=803)

    bot = SimpleNamespace(
        send_rich_message=AsyncMock(side_effect=send_rich),
        send_photo=AsyncMock(side_effect=send_photo),
        send_message=AsyncMock(side_effect=send_text),
    )
    pipeline = tenant_publish_pipeline.TenantPublishPipeline(bot=bot)
    method = SendRichMessage(chat_id=chat_id, rich_message=rich)

    def send():
        return pipeline._send_short(
            chat_id=chat_id,
            title="Test article",
            short_body=short_body,
            image_bytes=b"synthetic image",
        )

    def failure(kind):
        if kind == "timeout":
            return TelegramNetworkError(method=method, message="Request timeout error")
        if kind == "disconnect":
            return TelegramNetworkError(
                method=method, message="ServerDisconnectedError: response lost"
            )
        if kind == "server-error":
            return TelegramServerError(method=method, message="Internal Server Error")
        if kind == "decode-error":
            return ClientDecodeError(
                "Failed to decode object",
                ValueError("synthetic invalid response"),
                "synthetic response",
            )
        raise AssertionError(f"Unknown failure: {kind}")

    return SimpleNamespace(
        module=tenant_publish_pipeline,
        bot=bot,
        method=method,
        chat_id=chat_id,
        short_body=short_body,
        accepted=accepted,
        send=send,
        failure=failure,
    )


async def assert_fallback_sent(case, result):
    assert result == 802
    assert case.accepted == [("photo", 802), ("text", 803)]
    case.bot.send_photo.assert_awaited_once()
    case.bot.send_message.assert_awaited_once_with(
        chat_id=case.chat_id, text=case.short_body
    )


@pytest.mark.asyncio
async def test_successful_rich_send_does_not_send_fallback(fallback_case):
    case = fallback_case

    result = await case.send()

    assert result == 801
    assert case.accepted == [("rich", 801)]
    case.bot.send_rich_message.assert_awaited_once()
    case.bot.send_photo.assert_not_awaited()
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_rich_method_uses_fallback(fallback_case):
    case = fallback_case
    del case.bot.send_rich_message

    result = await case.send()

    await assert_fallback_sent(case, result)


@pytest.mark.asyncio
async def test_rejected_rich_format_uses_fallback(fallback_case):
    case = fallback_case
    case.bot.send_rich_message.side_effect = TelegramBadRequest(
        method=case.method, message="Bad Request: can't parse rich message"
    )

    result = await case.send()

    case.bot.send_rich_message.assert_awaited_once()
    await assert_fallback_sent(case, result)


@pytest.mark.asyncio
async def test_formatting_failure_before_send_uses_fallback(fallback_case, monkeypatch):
    case = fallback_case
    monkeypatch.setattr(
        case.module,
        "build_short_rich_message",
        Mock(side_effect=ValueError("synthetic formatting failure")),
    )

    result = await case.send()

    case.bot.send_rich_message.assert_not_awaited()
    await assert_fallback_sent(case, result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption", ["timeout", "disconnect", "server-error", "decode-error"]
)
async def test_ambiguous_rich_send_does_not_create_duplicate(
    fallback_case, interruption
):
    case = fallback_case
    failure = case.failure(interruption)

    async def accepted_but_response_lost(**kwargs):
        # Telegram may already have accepted the post before its reply is lost.
        case.accepted.append(("rich", 801))
        raise failure

    case.bot.send_rich_message.side_effect = accepted_but_response_lost
    caught = None
    try:
        await case.send()
    except (TelegramNetworkError, TelegramServerError, ClientDecodeError) as exc:
        caught = exc

    assert case.accepted == [("rich", 801)], (
        "После неопределённого ответа Telegram отправлен дополнительный пост"
    )
    assert caught is failure, (
        "Неопределённый результат отправки скрыт запасным способом"
    )
    case.bot.send_rich_message.assert_awaited_once()
    case.bot.send_photo.assert_not_awaited()
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_flood_limit_does_not_start_immediate_fallback(fallback_case):
    case = fallback_case
    failure = TelegramRetryAfter(
        method=case.method, message="Too Many Requests", retry_after=60
    )
    case.bot.send_rich_message.side_effect = failure
    caught = None
    try:
        await case.send()
    except TelegramRetryAfter as exc:
        caught = exc

    assert case.accepted == [], (
        "Telegram потребовал паузу, но бот сразу начал запасную отправку"
    )
    assert caught is failure, "Потеряна информация о требуемой паузе"
    assert caught.retry_after == 60
    case.bot.send_rich_message.assert_awaited_once()
    case.bot.send_photo.assert_not_awaited()
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_rich_send_does_not_start_fallback(fallback_case):
    case = fallback_case

    async def accepted_but_cancelled(**kwargs):
        case.accepted.append(("rich", 801))
        raise asyncio.CancelledError

    case.bot.send_rich_message.side_effect = accepted_but_cancelled
    with pytest.raises(asyncio.CancelledError):
        await case.send()

    assert case.accepted == [("rich", 801)]
    case.bot.send_photo.assert_not_awaited()
    case.bot.send_message.assert_not_awaited()
