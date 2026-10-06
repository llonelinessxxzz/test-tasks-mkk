import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from faststream.rabbit import RabbitBroker
from sqlalchemy import select

from app.models import Outbox, utcnow
from app.outbox import publish_next

pytestmark = pytest.mark.integration


async def test_publish_failure_keeps_event_pending(sessions, payment, settings) -> None:
    broker = AsyncMock(spec=RabbitBroker)
    broker.publish.side_effect = ConnectionError("RabbitMQ unavailable")
    with pytest.raises(ConnectionError):
        await publish_next(sessions, broker, settings)
    async with sessions() as session:
        event = (await session.scalars(select(Outbox))).one()
        assert event.published_at is None
    broker.publish.side_effect = None
    assert await publish_next(sessions, broker, settings)
    assert not await publish_next(sessions, broker, settings)
    async with sessions() as session:
        event = (await session.scalars(select(Outbox))).one()
        assert event.published_at is not None
    assert broker.publish.call_args.kwargs["persist"] is True
    assert broker.publish.call_args.kwargs["mandatory"] is True


async def test_lost_confirmation_allows_safe_redelivery(sessions, payment, settings) -> None:
    broker = AsyncMock(spec=RabbitBroker)
    broker.publish.side_effect = TimeoutError("published, confirmation lost")
    with pytest.raises(TimeoutError):
        await publish_next(sessions, broker, settings)
    broker.publish.side_effect = None
    assert await publish_next(sessions, broker, settings)
    first, second = broker.publish.call_args_list
    assert first.args == second.args
    assert first.kwargs["message_id"] == second.kwargs["message_id"]


async def test_delayed_events_are_not_published_early(sessions, payment, settings) -> None:
    async with sessions() as session, session.begin():
        event = (await session.scalars(select(Outbox))).one()
        event.available_at = utcnow() + timedelta(hours=1)
    broker = AsyncMock(spec=RabbitBroker)
    assert not await publish_next(sessions, broker, settings)
    broker.publish.assert_not_awaited()


async def test_parallel_publishers_do_not_publish_same_locked_event(
    sessions, payment, settings
) -> None:
    broker = AsyncMock(spec=RabbitBroker)

    async def publish(*args, **kwargs):
        await asyncio.sleep(0.1)

    broker.publish.side_effect = publish
    results = await asyncio.gather(*[publish_next(sessions, broker, settings) for _ in range(4)])
    assert results.count(True) == 1
    broker.publish.assert_awaited_once()
