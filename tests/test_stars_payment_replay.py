import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def payment_case(tmp_path, monkeypatch):
    import paid_multiuse
    import tenant_db

    path = tmp_path / "payments.sqlite"
    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    await tenant_db.init_db(path)
    user = SimpleNamespace(
        id=1001,
        username="test-user",
        first_name="Test",
        last_name=None,
        language_code="ru",
    )
    cfg = SimpleNamespace(default_publish_times=())
    monkeypatch.setattr(paid_multiuse, "_deps", lambda: (None, cfg, None))
    # Default topics and schedules are unrelated to payment replay protection.
    defaults = AsyncMock()
    monkeypatch.setattr(tenant_db, "ensure_defaults", defaults)
    expiry = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)

    def message(charge, expires_at, *, currency="XTR", payload="zen-sub:test"):
        payment = SimpleNamespace(
            currency=currency,
            invoice_payload=payload,
            total_amount=500,
            telegram_payment_charge_id=charge,
            provider_payment_charge_id="",
            subscription_expiration_date=(
                int(expires_at.timestamp()) if expires_at is not None else None
            ),
            is_recurring=expires_at is not None,
            is_first_recurring=True,
        )
        return SimpleNamespace(
            from_user=user, successful_payment=payment, answer=AsyncMock()
        )

    async def payment_count():
        async with aiosqlite.connect(path) as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM payments")
            return (await cursor.fetchone())[0]

    async def subscription():
        row = await tenant_db.subscription_info(user.id)
        return dict(row) if row is not None else None

    return SimpleNamespace(
        path=path,
        module=paid_multiuse,
        database=tenant_db,
        user=user,
        expiry=expiry,
        message=message,
        payment_count=payment_count,
        subscription=subscription,
        defaults=defaults,
    )


@pytest.mark.asyncio
async def test_first_stars_payment_activates_subscription(payment_case):
    case = payment_case
    message = case.message("first-charge", case.expiry)
    await case.module.successful_payment(message)

    subscription = await case.subscription()
    assert subscription["status"] == "active"
    assert subscription["expires_at"] == case.expiry.isoformat(timespec="seconds")
    assert subscription["source"] == "telegram_stars"
    assert subscription["telegram_payment_charge_id"] == "first-charge"
    assert await case.payment_count() == 1
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_replayed_payment_does_not_repeat_activation_message(payment_case):
    case = payment_case
    message = case.message("same-charge", case.expiry)
    await case.module.successful_payment(message)
    await case.module.successful_payment(message)

    assert await case.payment_count() == 1
    message.answer.assert_awaited_once()
    case.defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_payment_replay_preserves_newer_subscription(payment_case):
    case = payment_case
    old = case.message("old-charge", case.expiry)
    new_expiry = case.expiry + timedelta(days=30)
    newer = case.message("new-charge", new_expiry)

    await case.module.successful_payment(old)
    await case.module.successful_payment(newer)
    before = await case.subscription()
    await case.module.successful_payment(old)
    after = await case.subscription()

    assert before["expires_at"] == new_expiry.isoformat(timespec="seconds")
    assert after == before, "Повтор старого платежа изменил продлённую подписку"
    assert await case.payment_count() == 2


@pytest.mark.asyncio
async def test_out_of_order_payment_does_not_shorten_subscription(payment_case):
    case = payment_case
    newer = case.message("new-charge", case.expiry + timedelta(days=30))
    older = case.message("late-old-charge", case.expiry)

    await case.module.successful_payment(newer)
    before = await case.subscription()
    await case.module.successful_payment(older)

    assert await case.subscription() == before, (
        "Запоздалое уведомление сократило действующую подписку"
    )
    assert await case.payment_count() == 2


@pytest.mark.asyncio
async def test_concurrent_duplicate_payment_is_processed_once(payment_case):
    case = payment_case
    first = case.message("concurrent-charge", case.expiry)
    second = case.message("concurrent-charge", case.expiry)

    await asyncio.gather(
        case.module.successful_payment(first),
        case.module.successful_payment(second),
    )

    assert await case.payment_count() == 1
    assert first.answer.await_count + second.answer.await_count == 1
    case.defaults.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("currency", "payload"), [("RUB", "zen-sub:test"), ("XTR", "other-product")]
)
async def test_unrelated_payment_does_not_activate_subscription(
    payment_case, currency, payload
):
    case = payment_case
    message = case.message("unrelated", case.expiry, currency=currency, payload=payload)
    await case.module.successful_payment(message)

    assert await case.payment_count() == 0
    assert await case.subscription() is None
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_without_expiry_does_not_extend_access(payment_case, monkeypatch):
    case = payment_case
    initial_time = datetime(2030, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        current = initial_time

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    monkeypatch.setattr(case.module, "datetime", Clock)
    message = case.message("no-expiry-charge", None)
    await case.module.successful_payment(message)
    before = await case.subscription()
    Clock.current += timedelta(days=1)
    await case.module.successful_payment(message)

    assert await case.subscription() == before, (
        "Повтор платежа без даты окончания добавил лишний день доступа"
    )
    assert await case.payment_count() == 1


@pytest.mark.asyncio
async def test_subscription_write_failure_rolls_back_payment(payment_case):
    case = payment_case
    async with aiosqlite.connect(case.path) as connection:
        await connection.executescript(
            """
            CREATE TRIGGER reject_subscription BEFORE INSERT ON subscriptions
            BEGIN
                SELECT RAISE(ABORT, 'synthetic subscription write failure');
            END;
            """
        )

    message = case.message("failed-charge", case.expiry)
    with pytest.raises(
        aiosqlite.IntegrityError, match="synthetic subscription write failure"
    ):
        await case.module.successful_payment(message)

    assert await case.payment_count() == 0, (
        "Оплата сохранена отдельно от неудачной активации подписки"
    )
    assert await case.subscription() is None
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_database_failure_can_activate_subscription(payment_case):
    case = payment_case
    async with aiosqlite.connect(case.path) as connection:
        await connection.executescript(
            """
            CREATE TRIGGER reject_subscription BEFORE INSERT ON subscriptions
            BEGIN
                SELECT RAISE(ABORT, 'synthetic temporary failure');
            END;
            """
        )

    message = case.message("retry-charge", case.expiry)
    with pytest.raises(aiosqlite.IntegrityError, match="synthetic temporary failure"):
        await case.module.successful_payment(message)

    assert await case.payment_count() == 0
    async with aiosqlite.connect(case.path) as connection:
        await connection.execute("DROP TRIGGER reject_subscription")
        await connection.commit()

    await case.module.successful_payment(message)
    assert await case.payment_count() == 1
    subscription = await case.subscription()
    assert subscription["status"] == "active"
    assert subscription["expires_at"] == case.expiry.isoformat(timespec="seconds")
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_renewal_preserves_previous_subscription(payment_case):
    case = payment_case
    first = case.message("first-charge", case.expiry)
    await case.module.successful_payment(first)
    before = await case.subscription()
    async with aiosqlite.connect(case.path) as connection:
        await connection.executescript(
            """
            CREATE TRIGGER reject_renewal BEFORE UPDATE ON subscriptions
            BEGIN
                SELECT RAISE(ABORT, 'synthetic renewal failure');
            END;
            """
        )

    newer = case.message("renewal-charge", case.expiry + timedelta(days=30))
    with pytest.raises(aiosqlite.IntegrityError, match="synthetic renewal failure"):
        await case.module.successful_payment(newer)

    assert await case.payment_count() == 1
    assert await case.subscription() == before
    newer.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_distinct_payments_keep_longest_expiry(payment_case):
    case = payment_case
    new_expiry = case.expiry + timedelta(days=30)
    older = case.message("concurrent-old", case.expiry)
    newer = case.message("concurrent-new", new_expiry)

    await asyncio.gather(
        case.module.successful_payment(older),
        case.module.successful_payment(newer),
    )

    assert await case.payment_count() == 2
    subscription = await case.subscription()
    assert subscription["expires_at"] == new_expiry.isoformat(timespec="seconds")
    assert subscription["telegram_payment_charge_id"] == "concurrent-new"


@pytest.mark.asyncio
async def test_duplicate_payment_does_not_restore_revoked_access(payment_case):
    case = payment_case
    message = case.message("revoked-charge", case.expiry)
    await case.module.successful_payment(message)
    await case.database.revoke_subscription(case.user.id)
    before = await case.subscription()

    await case.module.successful_payment(message)

    assert await case.subscription() == before
    assert await case.payment_count() == 1
    assert not await case.database.is_subscription_active(case.user.id)
    message.answer.assert_awaited_once()
