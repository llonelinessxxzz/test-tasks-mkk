# Асинхронный процессинг платежей

## Запуск

```bash
cp .env.example .env
docker compose --profile demo up --build -d --wait
```

Миграции применяются автоматически. `demo` запускает тестовый приёмник webhook.

- API: `POST http://localhost:8000/api/v1/payments` — создание платежа через команду ниже.
- RabbitMQ: http://localhost:15672 — логин и пароль: `payments`.

## Быстрая проверка

```bash
docker compose exec -T api python -m scripts.smoke
```

Проверяет создание и получение платежей, идемпотентность, конфликт `409`, доставку webhook и три попытки при ошибке.

Ожидаются две строки `OK payment=...`: с `attempts=1` и `attempts=3`. Статус `failed` допустим: результат шлюза случайный.

Тестовые платежи остаются в базе. Сообщение для неудачного webhook попадает в `payments.dlq`.

## Примеры запросов

Проверка сервиса:

```bash
curl http://localhost:8000/health \
  -H 'X-API-Key: local-development-key-change-me'
```

Ожидаем: `{"status":"ok"}`.

Создание платежа:

```bash
curl -i http://localhost:8000/api/v1/payments \
  -H 'X-API-Key: local-development-key-change-me' \
  -H 'Idempotency-Key: order-112' \
  -H 'Content-Type: application/json' \
  -d '{"amount":"54543.00","currency":"RUB","description":"Заказ 112","metadata":{"order_id":112},"webhook_url":"http://webhook:8081/payments"}'
```

Ожидаем: `202 Accepted`, `payment_id`, `status`, `created_at`.
Повтор с тем же ключом и данными вернёт тот же платёж. Другие данные с тем же ключом - `409 Conflict`.

Получение платежа - заменить `UUID` на полученный `payment_id`:

```bash
curl http://localhost:8000/api/v1/payments/UUID \
  -H 'X-API-Key: local-development-key-change-me'
```

После обработки статус станет `succeeded` или `failed`. Шлюз эмулирует 90% успеха и 10% отказов.
`webhook_delivered_at` показывает доставку уведомления.

Если статус ещё `pending`, нужно повторить запрос через несколько секунд.

Для проверки повторов создайте платёж с новым `Idempotency-Key` и адресом `http://webhook:8081/fail`.
После трёх ошибок доставки сообщение попадёт в очередь `payments.dlq`. Статус самого платежа не изменится.

## Тесты, логи и остановка

Запуск тестов с отдельными PostgreSQL и RabbitMQ:

```bash
docker compose --profile test run --build --rm tests
```

Состояние и логи:

```bash
docker compose ps
docker compose logs -f api consumer webhook
```

Остановка с сохранением данных:

```bash
docker compose --profile demo --profile test down
```
