import asyncio
import logging

from faststream.rabbit import RabbitBroker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker import create_broker, declare_queues, exchange
from app.config import Settings
from app.models import Outbox, utcnow

logger = logging.getLogger(__name__)


async def publish_next(
    sessions: async_sessionmaker[AsyncSession], broker: RabbitBroker, settings: Settings
) -> bool:
    async with sessions() as session, session.begin():
        event = await session.scalar(
            select(Outbox)
            .where(Outbox.published_at.is_(None), Outbox.available_at <= utcnow())
            .order_by(Outbox.available_at, Outbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return False
        await broker.publish(
            event.payload,
            exchange=exchange,
            routing_key=event.queue,
            persist=True,
            mandatory=True,
            message_id=str(event.id),
            timeout=settings.publish_timeout_seconds,
        )
        event.published_at = utcnow()
        logger.info(
            "Published event=%s payment=%s queue=%s", event.id, event.payment_id, event.queue
        )
    return True


async def run_publisher(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    while True:
        try:
            async with create_broker(settings) as broker:
                await declare_queues(broker)
                while True:
                    if not await publish_next(sessions, broker, settings):
                        await asyncio.sleep(settings.outbox_poll_seconds)
        except Exception:
            logger.exception("Outbox publisher failed; reconnecting")
            await asyncio.sleep(settings.outbox_poll_seconds)
