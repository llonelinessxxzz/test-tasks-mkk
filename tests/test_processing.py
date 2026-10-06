import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.models import Outbox, Payment, utcnow
from app.processing import process_payment
from app.schemas import PaymentEvent

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_concurrent_duplicate_is_processed_once(
    sessions, payment, settings, monkeypatch, status
) -> None:
    gateway = AsyncMock(return_value=status)
    monkeypatch.setattr("app.processing.emulate_gateway", gateway)
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200)

    event = PaymentEvent(payment_id=payment.id, attempt=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        await asyncio.gather(
            *[process_payment(event, sessions, client, settings) for _ in range(4)]
        )
    gateway.assert_awaited_once()
    assert len(requests) == 1
    assert json.loads(requests[0].content)["status"] == status
    async with sessions() as session:
        stored = await session.get(Payment, payment.id)
        assert stored.status == status
        assert stored.webhook_delivered_at is not None
        assert stored.attempts == 1


async def test_webhook_retries_are_durable_and_end_in_dlq(
    sessions, payment, settings, monkeypatch
) -> None:
    gateway = AsyncMock(return_value="succeeded")
    monkeypatch.setattr("app.processing.emulate_gateway", gateway)
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        for attempt in range(3):
            started = utcnow()
            await process_payment(
                PaymentEvent(payment_id=payment.id, attempt=attempt), sessions, client, settings
            )
            async with sessions() as session:
                stored = await session.get(Payment, payment.id)
                assert stored.attempts == attempt + 1
                assert stored.status == "succeeded"
                events = list(
                    (await session.scalars(select(Outbox).order_by(Outbox.created_at))).all()
                )
                assert len(events) == attempt + 2
                latest = events[-1]
                if attempt < 2:
                    assert latest.queue == "payments.new"
                    assert latest.payload["attempt"] == attempt + 1
                    delay = (latest.available_at - started).total_seconds()
                    assert settings.retry_delay_seconds * 2**attempt <= delay < 10
                else:
                    assert latest.queue == "payments.dlq"
                    assert stored.exhausted_at is not None
                    assert stored.webhook_delivered_at is None
        await process_payment(
            PaymentEvent(payment_id=payment.id, attempt=2), sessions, client, settings
        )
    gateway.assert_awaited_once()
    assert len(requests) == 3
    assert len({request.headers["X-Event-ID"] for request in requests}) == 1
    assert len({request.content for request in requests}) == 1


async def test_retry_succeeds_without_repeating_gateway(
    sessions, payment, settings, monkeypatch
) -> None:
    gateway = AsyncMock(return_value="failed")
    monkeypatch.setattr("app.processing.emulate_gateway", gateway)
    statuses = iter([503, 200])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses)))
    ) as client:
        await process_payment(
            PaymentEvent(payment_id=payment.id, attempt=0), sessions, client, settings
        )
        await process_payment(
            PaymentEvent(payment_id=payment.id, attempt=0), sessions, client, settings
        )
        await process_payment(
            PaymentEvent(payment_id=payment.id, attempt=1), sessions, client, settings
        )
    gateway.assert_awaited_once()
    async with sessions() as session:
        stored = await session.get(Payment, payment.id)
        assert stored.status == "failed"
        assert stored.attempts == 2
        assert stored.last_error is None
        assert stored.webhook_delivered_at is not None
        assert stored.exhausted_at is None


async def test_crash_after_gateway_preserves_result(
    sessions, payment, settings, monkeypatch
) -> None:
    gateway = AsyncMock(return_value="succeeded")
    webhook = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("app.processing.emulate_gateway", gateway)
    monkeypatch.setattr("app.processing.send_webhook", webhook)
    event = PaymentEvent(payment_id=payment.id, attempt=0)
    async with httpx.AsyncClient() as client:
        with pytest.raises(asyncio.CancelledError):
            await process_payment(event, sessions, client, settings)
        async with sessions() as session:
            stored = await session.get(Payment, payment.id)
            assert stored.status == "succeeded"
            assert stored.attempts == 0
        webhook.side_effect = None
        await process_payment(event, sessions, client, settings)
    gateway.assert_awaited_once()
    assert webhook.await_count == 2


async def test_gateway_exception_retries_and_dead_letters(
    sessions, payment, settings, monkeypatch
) -> None:
    gateway = AsyncMock(side_effect=RuntimeError("gateway unavailable"))
    webhook = AsyncMock()
    monkeypatch.setattr("app.processing.emulate_gateway", gateway)
    monkeypatch.setattr("app.processing.send_webhook", webhook)
    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            await process_payment(
                PaymentEvent(payment_id=payment.id, attempt=attempt), sessions, client, settings
            )
    assert gateway.await_count == 3
    webhook.assert_not_awaited()
    async with sessions() as session:
        stored = await session.get(Payment, payment.id)
        assert stored.status == "pending"
        assert stored.exhausted_at is not None
        assert (
            len((await session.scalars(select(Outbox).where(Outbox.queue == "payments.dlq"))).all())
            == 1
        )


@pytest.mark.parametrize("error", ["timeout", "redirect"])
async def test_timeout_and_redirect_are_retried(
    sessions, payment, settings, monkeypatch, error
) -> None:
    monkeypatch.setattr("app.processing.emulate_gateway", AsyncMock(return_value="succeeded"))

    def receive(request):
        if error == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(302, headers={"Location": "https://another.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        await process_payment(
            PaymentEvent(payment_id=payment.id, attempt=0), sessions, client, settings
        )
    async with sessions() as session:
        stored = await session.get(Payment, payment.id)
        assert stored.status == "succeeded"
        assert stored.attempts == 1
        assert stored.webhook_delivered_at is None
