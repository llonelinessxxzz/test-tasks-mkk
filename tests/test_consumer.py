import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.consumer import handle_message


async def test_ack_happens_after_processing(monkeypatch, settings) -> None:
    events = []

    async def process(*args):
        events.append("processed")

    async def ack():
        events.append("acknowledged")

    monkeypatch.setattr("app.consumer.process_payment", process)
    message = AsyncMock()
    message.ack.side_effect = ack
    await handle_message(
        {"payment_id": str(uuid4()), "attempt": 0},
        message,
        AsyncMock(),
        Mock(),
        AsyncMock(),
        settings,
    )
    assert events == ["processed", "acknowledged"]
    message.nack.assert_not_awaited()


async def test_database_failure_requeues_without_ack(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        "app.consumer.process_payment", AsyncMock(side_effect=ConnectionError("database offline"))
    )
    monkeypatch.setattr("app.consumer.asyncio.sleep", AsyncMock())
    message = AsyncMock()
    await handle_message(
        {"payment_id": str(uuid4()), "attempt": 0},
        message,
        AsyncMock(),
        Mock(),
        AsyncMock(),
        settings,
    )
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.parametrize("body", [{"bad": "event"}, b"not json", {"payment_id": "invalid"}])
async def test_invalid_message_is_dead_lettered_before_ack(body, settings) -> None:
    broker = AsyncMock()
    message = AsyncMock()
    await handle_message(body, message, broker, Mock(), AsyncMock(), settings)
    broker.publish.assert_awaited_once()
    call = broker.publish.call_args
    assert call.kwargs["routing_key"] == "payments.dlq"
    assert call.kwargs["persist"]
    json.dumps(call.args[0])
    message.ack.assert_awaited_once()


async def test_failed_dlq_publish_requeues_original(monkeypatch, settings) -> None:
    broker = AsyncMock()
    broker.publish.side_effect = ConnectionError("broker offline")
    message = AsyncMock()
    monkeypatch.setattr("app.consumer.asyncio.sleep", AsyncMock())
    await handle_message({"bad": "event"}, message, broker, Mock(), AsyncMock(), settings)
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)
