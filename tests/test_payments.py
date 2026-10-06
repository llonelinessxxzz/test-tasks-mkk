import asyncio
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import Outbox, Payment
from app.payments import create_payment
from app.schemas import PaymentCreate

pytestmark = pytest.mark.integration


async def test_create_and_read(api, sessions, payload) -> None:
    response = await api.post(
        "/api/v1/payments", json=payload, headers={"Idempotency-Key": "order-1"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["created_at"].endswith("Z") or body["created_at"].endswith("+00:00")
    response = await api.get(f"/api/v1/payments/{body['payment_id']}")
    assert response.status_code == 200
    assert response.json()["amount"] == "1250.50"
    assert response.json()["metadata"] == {"order_id": 42}
    assert response.json()["processed_at"] is None
    async with sessions() as session:
        event = (await session.scalars(select(Outbox))).one()
        assert event.payload == {"payment_id": body["payment_id"], "attempt": 0}
        assert event.published_at is None


async def test_concurrent_idempotency(api, sessions, payload) -> None:
    responses = await asyncio.gather(
        *[
            api.post("/api/v1/payments", json=payload, headers={"Idempotency-Key": "same-key"})
            for _ in range(12)
        ]
    )
    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["payment_id"] for response in responses}) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1


async def test_concurrent_conflicting_payloads(api, payload) -> None:
    responses = await asyncio.gather(
        api.post("/api/v1/payments", json=payload, headers={"Idempotency-Key": "same-key"}),
        api.post(
            "/api/v1/payments",
            json={**payload, "amount": "2.00"},
            headers={"Idempotency-Key": "same-key"},
        ),
    )
    assert sorted(response.status_code for response in responses) == [202, 409]


@pytest.mark.parametrize("field", ["amount", "currency", "description", "metadata", "webhook_url"])
async def test_conflict_on_changed_payload(api, payload, field) -> None:
    headers = {"Idempotency-Key": "same-key"}
    assert (await api.post("/api/v1/payments", json=payload, headers=headers)).status_code == 202
    changes = {
        "amount": "100.00",
        "currency": "USD",
        "description": "Another order",
        "metadata": {"order_id": 43},
        "webhook_url": "https://other.example/webhook",
    }
    payload[field] = changes[field]
    assert (await api.post("/api/v1/payments", json=payload, headers=headers)).status_code == 409


async def test_metadata_boolean_is_not_integer(api, payload) -> None:
    headers = {"Idempotency-Key": "same-key"}
    payload["metadata"] = {"flag": True}
    assert (await api.post("/api/v1/payments", json=payload, headers=headers)).status_code == 202
    payload["metadata"] = {"flag": 1}
    assert (await api.post("/api/v1/payments", json=payload, headers=headers)).status_code == 409


async def test_normalized_amount_and_metadata_order(api, payload) -> None:
    headers = {"Idempotency-Key": "same-key"}
    payload.update(amount="1.2", metadata={"a": 1, "b": 2})
    first = await api.post("/api/v1/payments", json=payload, headers=headers)
    payload.update(amount="1.20", metadata={"b": 2, "a": 1})
    second = await api.post("/api/v1/payments", json=payload, headers=headers)
    assert first.json() == second.json()


async def test_payment_and_outbox_are_atomic(sessions, payload) -> None:
    async with sessions() as session:
        with patch.object(session, "add", side_effect=RuntimeError("outbox write failed")):
            with pytest.raises(RuntimeError):
                await create_payment(session, PaymentCreate(**payload), "order-1")
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_not_found_and_invalid_uuid(api) -> None:
    assert (await api.get(f"/api/v1/payments/{uuid4()}")).status_code == 404
    assert (await api.get("/api/v1/payments/invalid")).status_code == 422


@pytest.mark.parametrize("key", [None, "", "   ", "a b", "x" * 201])
async def test_idempotency_key_is_required(api, payload, key) -> None:
    headers = {} if key is None else {"Idempotency-Key": key}
    assert (await api.post("/api/v1/payments", json=payload, headers=headers)).status_code == 422
