from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlunsplit

import httpx
import pytest
from pydantic import ValidationError

from app.main import app
from app.processing import emulate_gateway
from app.schemas import PaymentCreate


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "0"),
        ("amount", "-1"),
        ("amount", "0.001"),
        ("amount", "10000000000000000"),
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("currency", "GBP"),
        ("description", "x" * 1001),
        ("metadata", [1, 2]),
        ("metadata", {"x": float("inf")}),
        ("webhook_url", "ftp://example.com"),
        ("webhook_url", "https://user:pass@example.com"),
        ("webhook_url", urlunsplit(("https", "example.com", "/", "", "secret"))),
        ("extra", "value"),
    ],
)
def test_invalid_payment(payload: dict, field: str, value: object) -> None:
    payload[field] = value
    with pytest.raises(ValidationError):
        PaymentCreate(**payload)


def test_money_remains_decimal(payload: dict) -> None:
    payload["amount"] = "1.2"
    assert PaymentCreate(**payload).amount == Decimal("1.20")
    assert PaymentCreate(**payload).model_dump(mode="json")["amount"] == "1.20"


@pytest.mark.parametrize(
    "path", ["/health", "/api/v1/payments/00000000-0000-0000-0000-000000000000"]
)
@pytest.mark.parametrize("key", [None, "incorrect"])
async def test_get_requires_api_key(path: str, key: str | None) -> None:
    headers = {} if key is None else {"X-API-Key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == 401


async def test_create_requires_api_key(payload: dict) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/payments", json=payload)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("chance", "expected"), [(0.0, "succeeded"), (0.899, "succeeded"), (0.9, "failed")]
)
async def test_gateway_probability_and_delay(monkeypatch, chance: float, expected: str) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("app.processing.asyncio.sleep", sleep)
    monkeypatch.setattr("app.processing.random.random", lambda: chance)
    assert await emulate_gateway() == expected
    assert 2 <= sleep.call_args.args[0] <= 5
