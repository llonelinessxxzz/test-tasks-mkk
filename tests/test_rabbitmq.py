import json
import os

import pytest
from aio_pika.exceptions import DeliveryError
from sqlalchemy import select

from app.broker import create_broker, declare_queues, payments_queue
from app.models import Outbox
from app.outbox import publish_next

pytestmark = pytest.mark.integration


@pytest.fixture
async def broker(settings):
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("TEST_RABBITMQ_URL is required for RabbitMQ integration tests")
    settings = settings.model_copy(update={"rabbitmq_url": url})
    async with create_broker(settings) as connection:
        await declare_queues(connection)
        queue = await connection.declare_queue(payments_queue)
        await queue.purge()
        yield connection
        await queue.purge()


async def test_real_publish_confirm_and_redelivery(sessions, payment, settings, broker) -> None:
    assert await publish_next(sessions, broker, settings)
    queue = await broker.declare_queue(payments_queue)
    message = await queue.get(timeout=5)
    assert json.loads(message.body) == {"payment_id": str(payment.id), "attempt": 0}
    assert message.delivery_mode == 2
    message_id = message.message_id
    await message.reject(requeue=True)
    redelivery = await queue.get(timeout=5)
    assert redelivery.redelivered
    assert redelivery.message_id == message_id
    await redelivery.ack()
    async with sessions() as session:
        assert (await session.scalars(select(Outbox))).one().published_at is not None


async def test_unroutable_publish_is_not_marked_sent(sessions, payment, settings, broker) -> None:
    async with sessions() as session, session.begin():
        event = (await session.scalars(select(Outbox))).one()
        event.queue = "payments.does-not-exist"
    with pytest.raises(DeliveryError):
        await publish_next(sessions, broker, settings)
    async with sessions() as session:
        assert (await session.scalars(select(Outbox))).one().published_at is None
