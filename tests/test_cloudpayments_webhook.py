import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def cloud_case(tmp_path, monkeypatch):
    import cloudpayments_service as payments
    import tenant_db

    path = tmp_path / "cloudpayments.sqlite"
    now = datetime(2030, 1, 1, tzinfo=UTC)
    secret = "synthetic-api-secret-only"
    user_id = 1001
    monkeypatch.setattr(tenant_db, "DB_PATH", path)
    monkeypatch.setattr(tenant_db, "_DB_LOCK", asyncio.Lock())
    monkeypatch.setattr(payments, "_db_path", lambda: path)
    monkeypatch.setattr(payments, "_api_secret", lambda: secret)
    monkeypatch.setattr(payments, "_price_rub", lambda: 1200)
    monkeypatch.setattr(payments, "_now", lambda: now)
    await tenant_db.init_db(path)
    await tenant_db.touch_user(
        SimpleNamespace(
            id=user_id,
            username="synthetic-user",
            first_name="Test",
            last_name=None,
            language_code="ru",
        )
    )
    await payments.ensure_schema()
    bot = SimpleNamespace(send_message=AsyncMock())
    server = payments.CloudPaymentsWebhookServer(bot=bot)

    async def execute(sql, parameters=()):
        async with aiosqlite.connect(path) as connection:
            await connection.execute(sql, parameters)
            await connection.commit()

    async def rows(table):
        assert table in {
            "cloudpayments_orders",
            "subscriptions",
            "promo_redemptions",
            "user_promo_codes",
        }
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            return [dict(row) for row in await cursor.fetchall()]

    async def snapshot():
        return {
            table: await rows(table)
            for table in (
                "cloudpayments_orders",
                "subscriptions",
                "promo_redemptions",
                "user_promo_codes",
            )
        }

    async def add_order(invoice="invoice-1"):
        await execute(
            "INSERT INTO cloudpayments_orders "
            "(invoice_id, user_id, amount_kopecks, currency, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invoice, user_id, 120000, "RUB", "created", now.isoformat()),
        )

    def payload(**overrides):
        return {
            "TransactionId": "transaction-1",
            "InvoiceId": "invoice-1",
            "AccountId": str(user_id),
            "Amount": "1200.00",
            "Currency": "RUB",
            **overrides,
        }

    def request(data=None, *, encoding="json", raw=None, signing_secret=secret):
        if raw is None:
            data = payload() if data is None else data
            text = json.dumps(data) if encoding == "json" else urlencode(data)
            raw = text.encode("utf-8")
        signature = base64.b64encode(
            hmac.new(signing_secret.encode("utf-8"), raw, hashlib.sha256).digest()
        ).decode("ascii")
        return SimpleNamespace(
            read=AsyncMock(return_value=raw),
            headers={"Content-HMAC": signature},
            content_type=(
                "application/json"
                if encoding == "json"
                else "application/x-www-form-urlencoded"
            ),
        )

    async def add_promo():
        await payments.create_promo_code("TEST25", 25)
        await payments.apply_user_promo(user_id, "TEST25")
        await execute(
            "UPDATE cloudpayments_orders SET amount_kopecks=90000 WHERE invoice_id=?",
            ("invoice-1",),
        )
        await execute(
            "INSERT INTO cloudpayments_order_promos "
            "(invoice_id, code, discount_percent, base_amount_kopecks, discount_kopecks) "
            "VALUES (?, ?, ?, ?, ?)",
            ("invoice-1", "TEST25", 25, 120000, 30000),
        )

    await add_order()
    return SimpleNamespace(
        module=payments,
        database=tenant_db,
        now=now,
        user_id=user_id,
        bot=bot,
        server=server,
        execute=execute,
        rows=rows,
        snapshot=snapshot,
        add_order=add_order,
        add_promo=add_promo,
        payload=payload,
        request=request,
    )


def assert_response(response, *, status=200, code=0):
    assert response.status == status
    assert json.loads(response.text) == {"code": code}


async def assert_subscription(case, expires_at):
    subscriptions = await case.rows("subscriptions")
    assert len(subscriptions) == 1
    assert subscriptions[0]["user_id"] == case.user_id
    assert subscriptions[0]["status"] == "active"
    assert subscriptions[0]["source"] == "cloudpayments"
    assert subscriptions[0]["expires_at"] == expires_at.isoformat(timespec="seconds")


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["json", "form"])
async def test_valid_signed_payment_is_saved_before_notification(cloud_case, encoding):
    case = cloud_case

    async def inspect_notification(*args, **kwargs):
        await assert_subscription(case, case.now + timedelta(days=30))
        order = (await case.rows("cloudpayments_orders"))[0]
        assert order["status"] == "paid"
        assert order["transaction_id"] == "transaction-1"
        assert order["raw_payload"]

    case.bot.send_message.side_effect = inspect_notification
    assert_response(await case.server.pay(case.request(encoding=encoding)))
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signature_case", ["missing", "invalid", "wrong-key", "tamper"]
)
async def test_invalid_signature_cannot_change_payment_data(cloud_case, signature_case):
    case = cloud_case
    before = await case.snapshot()
    request = case.request()
    if signature_case == "missing":
        request.headers.clear()
    elif signature_case == "invalid":
        request.headers["Content-HMAC"] = "not-a-valid-signature"
    elif signature_case == "wrong-key":
        request = case.request(signing_secret="another-synthetic-secret")
    else:
        request.read.return_value = request.read.return_value + b" "

    assert_response(await case.server.pay(request), status=403, code=13)
    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_secret_cannot_accept_payment(cloud_case, monkeypatch):
    case = cloud_case
    before = await case.snapshot()
    monkeypatch.setattr(case.module, "_api_secret", lambda: "")
    request = case.request()

    assert_response(await case.server.pay(request), status=503, code=13)
    request.read.assert_not_awaited()
    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("TransactionId", ""),
        ("InvoiceId", ""),
        ("AccountId", ""),
        ("Amount", ""),
        ("Currency", ""),
        ("InvoiceId", "unknown-invoice"),
        ("AccountId", "9999"),
        ("Amount", "1199.99"),
        ("Currency", "USD"),
        ("Amount", "not-a-number"),
    ],
)
async def test_invalid_payment_fields_leave_database_unchanged(
    cloud_case, field, value
):
    case = cloud_case
    before = await case.snapshot()
    request = case.request(case.payload(**{field: value}))

    assert_response(await case.server.pay(request), status=400, code=13)
    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"{", b"[]", b"\xff"])
async def test_signed_malformed_payload_is_rejected(cloud_case, raw):
    case = cloud_case
    before = await case.snapshot()

    assert_response(await case.server.pay(case.request(raw=raw)), status=400, code=13)
    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("order_status", ["create_failed", "cancelled"])
async def test_nonpayable_order_is_rejected(cloud_case, order_status):
    case = cloud_case
    await case.execute(
        "UPDATE cloudpayments_orders SET status=? WHERE invoice_id=?",
        (order_status, "invoice-1"),
    )
    before = await case.snapshot()

    assert_response(await case.server.pay(case.request()), status=400, code=13)
    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_replay_does_not_extend_subscription(cloud_case, monkeypatch):
    case = cloud_case
    assert_response(await case.server.pay(case.request()))
    before = await case.snapshot()
    monkeypatch.setattr(case.module, "_now", lambda: case.now + timedelta(days=10))

    assert_response(await case.server.pay(case.request()))
    assert await case.snapshot() == before
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_duplicate_notification_activates_once(cloud_case):
    case = cloud_case
    responses = await asyncio.gather(
        case.server.pay(case.request()), case.server.pay(case.request())
    )

    for response in responses:
        assert_response(response)
    await assert_subscription(case, case.now + timedelta(days=30))
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_distinct_orders_preserve_both_extensions(cloud_case):
    case = cloud_case
    await case.add_order("invoice-2")
    responses = await asyncio.gather(
        case.server.pay(case.request()),
        case.server.pay(
            case.request(
                case.payload(InvoiceId="invoice-2", TransactionId="transaction-2")
            )
        ),
    )

    for response in responses:
        assert_response(response)
    await assert_subscription(case, case.now + timedelta(days=60))
    orders = await case.rows("cloudpayments_orders")
    assert all(order["status"] == "paid" for order in orders)
    assert {order["transaction_id"] for order in orders} == {
        "transaction-1",
        "transaction-2",
    }
    assert case.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_transaction_cannot_pay_another_invoice(cloud_case):
    case = cloud_case
    await case.add_order("invoice-2")
    assert_response(await case.server.pay(case.request()))
    before = await case.snapshot()

    request = case.request(case.payload(InvoiceId="invoice-2"))
    assert_response(await case.server.pay(request), status=400, code=13)
    assert await case.snapshot() == before
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_payment_extends_existing_active_subscription(cloud_case):
    case = cloud_case
    expiry = case.now + timedelta(days=20)
    await case.database.activate_subscription(
        case.user_id, expires_at=expiry, source="test"
    )

    assert_response(await case.server.pay(case.request()))
    await assert_subscription(case, expiry + timedelta(days=30))
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_paid_promo_is_redeemed_once_and_pending_choice_cleared(cloud_case):
    case = cloud_case
    await case.add_promo()
    request = case.request(case.payload(Amount="900.00"))
    assert_response(await case.server.pay(request))
    before = await case.snapshot()

    assert len(before["promo_redemptions"]) == 1
    redemption = before["promo_redemptions"][0]
    assert redemption["invoice_id"] == "invoice-1"
    assert redemption["transaction_id"] == "transaction-1"
    assert redemption["discount_kopecks"] == 30000
    assert before["user_promo_codes"] == []
    await assert_subscription(case, case.now + timedelta(days=30))
    assert_response(await case.server.pay(case.request(case.payload(Amount="900.00"))))
    assert await case.snapshot() == before
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_notification_failure_does_not_undo_paid_order(cloud_case):
    case = cloud_case
    case.bot.send_message.side_effect = RuntimeError("synthetic notification failure")

    assert_response(await case.server.pay(case.request()))
    await assert_subscription(case, case.now + timedelta(days=30))
    before = await case.snapshot()
    assert before["cloudpayments_orders"][0]["status"] == "paid"
    assert_response(await case.server.pay(case.request()))
    assert await case.snapshot() == before
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["order", "promo"])
async def test_storage_failure_rolls_back_payment_and_allows_retry(
    cloud_case, failure_point
):
    case = cloud_case
    await case.add_promo()
    before = await case.snapshot()
    target = (
        "BEFORE UPDATE OF status ON cloudpayments_orders WHEN NEW.status='paid'"
        if failure_point == "order"
        else "BEFORE INSERT ON promo_redemptions"
    )
    await case.execute(
        f"CREATE TRIGGER synthetic_payment_failure {target} "
        "BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END"
    )
    request = case.request(case.payload(Amount="900.00"))

    assert_response(await case.server.pay(request), status=400, code=13)
    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()

    await case.execute("DROP TRIGGER synthetic_payment_failure")
    assert_response(await case.server.pay(case.request(case.payload(Amount="900.00"))))
    await assert_subscription(case, case.now + timedelta(days=30))
    assert len(await case.rows("promo_redemptions")) == 1
    assert await case.rows("user_promo_codes") == []
    case.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_before_order_save_rolls_back_subscription(
    cloud_case, monkeypatch, created_tasks
):
    case = cloud_case
    before = await case.snapshot()
    saving = asyncio.Event()
    original_execute = aiosqlite.Connection.execute

    async def delayed_execute(connection, sql, *args, **kwargs):
        if "UPDATE cloudpayments_orders" in sql and "status='paid'" in sql:
            saving.set()
            await asyncio.Event().wait()
        return await original_execute(connection, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", delayed_execute)
    task = asyncio.create_task(case.server.pay(case.request()))
    created_tasks.append(task)
    await asyncio.wait_for(saving.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await case.snapshot() == before
    case.bot.send_message.assert_not_awaited()
    monkeypatch.setattr(aiosqlite.Connection, "execute", original_execute)
    assert_response(await case.server.pay(case.request()))
    await assert_subscription(case, case.now + timedelta(days=30))


@pytest.mark.asyncio
async def test_cancellation_during_notification_keeps_committed_payment(
    cloud_case, created_tasks
):
    case = cloud_case
    notifying = asyncio.Event()

    async def delayed_notification(*args, **kwargs):
        notifying.set()
        await asyncio.Event().wait()

    case.bot.send_message.side_effect = delayed_notification
    task = asyncio.create_task(case.server.pay(case.request()))
    created_tasks.append(task)
    await asyncio.wait_for(notifying.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_subscription(case, case.now + timedelta(days=30))
    before = await case.snapshot()
    assert before["cloudpayments_orders"][0]["status"] == "paid"
    assert_response(await case.server.pay(case.request()))
    assert await case.snapshot() == before
    case.bot.send_message.assert_awaited_once()
