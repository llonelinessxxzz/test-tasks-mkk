import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Outbox, Payment
from app.schemas import PaymentCreate


class IdempotencyConflict(Exception):
    pass


async def create_payment(
    session: AsyncSession, data: PaymentCreate, idempotency_key: str
) -> Payment:
    payment_id = uuid4()
    async with session.begin():
        statement = (
            insert(Payment)
            .values(
                id=payment_id,
                amount=data.amount,
                currency=data.currency,
                description=data.description,
                details=data.metadata,
                webhook_url=str(data.webhook_url),
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
            .returning(Payment.id)
        )
        created_id = await session.scalar(statement)
        payment = (
            await session.scalars(select(Payment).where(Payment.idempotency_key == idempotency_key))
        ).one()
        if created_id is not None:
            session.add(
                Outbox(payment_id=payment.id, payload={"payment_id": str(payment.id), "attempt": 0})
            )
        elif (
            payment.amount != data.amount
            or payment.currency != data.currency
            or payment.description != data.description
            or json.dumps(payment.details, sort_keys=True)
            != json.dumps(data.metadata, sort_keys=True)
            or payment.webhook_url != str(data.webhook_url)
        ):
            raise IdempotencyConflict
    return payment


async def get_payment(session: AsyncSession, payment_id: UUID) -> Payment | None:
    return await session.get(Payment, payment_id)
