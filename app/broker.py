from faststream.rabbit import Channel, RabbitBroker, RabbitExchange, RabbitQueue

from app.config import Settings

exchange = RabbitExchange("payments", durable=True)
payments_queue = RabbitQueue("payments.new", durable=True)
dead_queue = RabbitQueue("payments.dlq", durable=True)


def create_broker(settings: Settings) -> RabbitBroker:
    return RabbitBroker(
        settings.rabbitmq_url,
        default_channel=Channel(prefetch_count=1, publisher_confirms=True, on_return_raises=True),
    )


async def declare_queues(broker: RabbitBroker) -> None:
    declared_exchange = await broker.declare_exchange(exchange)
    for queue in (payments_queue, dead_queue):
        declared_queue = await broker.declare_queue(queue)
        await declared_queue.bind(declared_exchange, routing_key=queue.name)
