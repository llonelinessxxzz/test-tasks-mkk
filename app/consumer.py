import asyncio
import logging
from typing import Any

import httpx
from faststream import AckPolicy, FastStream
from faststream.rabbit import RabbitBroker, RabbitMessage
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker import create_broker, dead_queue, declare_queues, exchange, payments_queue
from app.config import Settings, get_settings
from app.database import get_engine, get_session_factory
from app.processing import UnknownPaymentError, process_payment
from app.schemas import PaymentEvent

logger = logging.getLogger(__name__)


async def handle_message(
    body: Any,
    message: RabbitMessage,
    broker: RabbitBroker,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    try:
        try:
            event = PaymentEvent.model_validate(body)
            await process_payment(event, sessions, client, settings)
        except (ValidationError, UnknownPaymentError) as error:
            await broker.publish(
                {"error": str(error)[:2000], "payload": str(body)[:4000]},
                exchange=exchange,
                routing_key=dead_queue.name,
                persist=True,
                mandatory=True,
                timeout=settings.publish_timeout_seconds,
            )
        await message.ack()
    except Exception:
        logger.exception("Message processing failed; requeueing")
        await asyncio.sleep(1)
        await message.nack(requeue=True)


async def run() -> None:
    settings = get_settings()
    broker = create_broker(settings)
    sessions = get_session_factory()
    async with httpx.AsyncClient(
        timeout=settings.webhook_timeout_seconds, follow_redirects=False, trust_env=False
    ) as client:
        application = FastStream(broker)

        @application.on_startup
        async def setup() -> None:
            await broker.connect()
            await declare_queues(broker)

        @broker.subscriber(payments_queue, exchange, ack_policy=AckPolicy.MANUAL)
        async def handle(body: Any, message: RabbitMessage) -> None:
            await handle_message(body, message, broker, sessions, client, settings)

        try:
            await application.run()
        finally:
            await get_engine().dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
