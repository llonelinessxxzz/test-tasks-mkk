import argparse
import asyncio
from uuid import uuid4

import httpx

from app.config import get_settings


async def check_payment(client: httpx.AsyncClient, webhook_url: str, failure: bool) -> None:
    payload = {
        "amount": "12.50",
        "currency": "RUB",
        "description": "Smoke test",
        "metadata": {"smoke": True},
        "webhook_url": webhook_url,
    }
    headers = {"Idempotency-Key": f"smoke-{uuid4()}"}
    response = await client.post("/api/v1/payments", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    payment_id = response.json()["payment_id"]
    duplicate = await client.post("/api/v1/payments", json=payload, headers=headers)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["payment_id"] == payment_id
    conflict = await client.post(
        "/api/v1/payments", json={**payload, "amount": "99.00"}, headers=headers
    )
    assert conflict.status_code == 409, conflict.text
    for _ in range(60):
        response = await client.get(f"/api/v1/payments/{payment_id}")
        response.raise_for_status()
        payment = response.json()
        terminal = payment["exhausted_at"] if failure else payment["webhook_delivered_at"]
        if terminal:
            assert payment["status"] in {"succeeded", "failed"}
            assert payment["attempts"] == (3 if failure else 1)
            print(
                f"OK payment={payment_id} status={payment['status']} attempts={payment['attempts']}"
            )
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"Payment did not finish: {payment_id}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--webhook-url", default="http://webhook:8081/payments")
    parser.add_argument("--failure-url", default="http://webhook:8081/fail")
    args = parser.parse_args()
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers={"X-API-Key": get_settings().api_key},
        timeout=10,
        trust_env=False,
    ) as client:
        (await client.get("/health")).raise_for_status()
        await check_payment(client, args.webhook_url, False)
        await check_payment(client, args.failure_url, True)


if __name__ == "__main__":
    asyncio.run(main())
