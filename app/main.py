import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_engine, get_session, get_session_factory
from app.outbox import run_publisher
from app.payments import IdempotencyConflict, create_payment, get_payment
from app.schemas import PaymentAccepted, PaymentCreate, PaymentDetails

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    publisher = asyncio.create_task(run_publisher(get_session_factory(), settings))
    try:
        yield
    finally:
        publisher.cancel()
        with suppress(asyncio.CancelledError):
            await publisher
        await get_engine().dispose()


async def authenticate(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().api_key.encode()
    if x_api_key is None or not compare_digest(x_api_key.encode(), expected):
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(
    title="Payment processing",
    lifespan=lifespan,
    dependencies=[Depends(authenticate)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
Session = Annotated[AsyncSession, Depends(get_session)]


@app.get("/health")
async def health(session: Session) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/api/v1/payments", status_code=202, response_model=PaymentAccepted)
async def post_payment(
    data: PaymentCreate,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200, pattern=r"^\S+$")],
) -> dict[str, object]:
    try:
        payment = await create_payment(session, data, idempotency_key)
    except IdempotencyConflict:
        raise HTTPException(
            status_code=409, detail="Idempotency key has a different payload"
        ) from None
    return {"payment_id": payment.id, "status": payment.status, "created_at": payment.created_at}


@app.get("/api/v1/payments/{payment_id}", response_model=PaymentDetails)
async def read_payment(payment_id: UUID, session: Session) -> dict[str, object]:
    payment = await get_payment(session, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {
        "payment_id": payment.id,
        "status": payment.status,
        "amount": payment.amount,
        "currency": payment.currency,
        "description": payment.description,
        "metadata": payment.details,
        "idempotency_key": payment.idempotency_key,
        "webhook_url": payment.webhook_url,
        "created_at": payment.created_at,
        "processed_at": payment.processed_at,
        "attempts": payment.attempts,
        "webhook_delivered_at": payment.webhook_delivered_at,
        "exhausted_at": payment.exhausted_at,
    }
