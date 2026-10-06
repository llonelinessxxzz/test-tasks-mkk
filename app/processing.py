import asyncio
import logging
import random
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Outbox, Payment, utcnow
from app.schemas import PaymentEvent

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class UnknownPaymentError(Exception):
    pass


async def emulate_gateway() -> str:
    await asyncio.sleep(random.uniform(2, 5))
    return "succeeded" if random.random() < 0.9 else "failed"


def is_finished(payment: Payment, event: PaymentEvent) -> bool:
    return (
        payment.webhook_delivered_at is not None
        or payment.exhausted_at is not None
        or payment.attempts != event.attempt
    )


def schedule_failure(
    session: AsyncSession, payment: Payment, error: Exception, settings: Settings
) -> None:
    payment.attempts += 1
    payment.last_error = f"{type(error).__name__}: {error}"[:2000]
    payload = {"payment_id": str(payment.id), "attempt": payment.attempts}
    available_at = utcnow()
    queue = "payments.new"
    if payment.attempts >= MAX_ATTEMPTS:
        payment.exhausted_at = available_at
        queue = "payments.dlq"
        payload["error"] = payment.last_error
    else:
        available_at += timedelta(
            seconds=settings.retry_delay_seconds * 2 ** (payment.attempts - 1)
        )
    session.add(
        Outbox(payment_id=payment.id, queue=queue, payload=payload, available_at=available_at)
    )
    logger.warning(
        "Attempt failed payment=%s attempt=%s queue=%s", payment.id, payment.attempts, queue
    )


async def send_webhook(client: httpx.AsyncClient, payment: Payment) -> None:
    event_id = str(payment.id)
    response = await client.post(
        payment.webhook_url,
        headers={"X-Event-ID": event_id, "Idempotency-Key": event_id},
        json={
            "event_id": event_id,
            "event": "payment.processed",
            "payment_id": str(payment.id),
            "status": payment.status,
            "amount": format(payment.amount, ".2f"),
            "currency": payment.currency,
            "description": payment.description,
            "metadata": payment.details,
            "created_at": payment.created_at.isoformat(),
            "processed_at": payment.processed_at.isoformat() if payment.processed_at else None,
        },
    )
    response.raise_for_status()


async def process_payment(
    event: PaymentEvent,
    sessions: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    async with sessions() as session:
        async with session.begin():
            payment = await session.scalar(
                select(Payment).where(Payment.id == event.payment_id).with_for_update()
            )
            if payment is None:
                raise UnknownPaymentError(f"Unknown payment: {event.payment_id}")
            if is_finished(payment, event):
                return
            if payment.status == "pending":
                try:
                    payment.status = await emulate_gateway()
                except Exception as error:
                    schedule_failure(session, payment, error, settings)
                    return
                payment.processed_at = utcnow()

        async with session.begin():
            await session.refresh(payment, with_for_update=True)
            if is_finished(payment, event):
                return
            try:
                await send_webhook(client, payment)
            except Exception as error:
                schedule_failure(session, payment, error, settings)
            else:
                payment.attempts += 1
                payment.webhook_delivered_at = utcnow()
                payment.last_error = None
                logger.info("Webhook delivered payment=%s status=%s", payment.id, payment.status)
