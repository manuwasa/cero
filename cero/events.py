"""
In-process pubsub for decoupling workers from the brain and UI.

Topics are strings — by convention "<kind>:<symbol>:<detail>", e.g.
"candle:BTC/USDT:USDT:1h" or "signal:new". Subscribers consume from a queue
they own, so a slow subscriber can't block publishers.

This is **best-effort**: if a subscriber's queue is full, the message is
dropped and a warning is logged. Workers should always write to the DB first
(durable) and publish second (notify).

Usage:
    bus = EventBus()
    q = bus.subscribe("candle:BTC/USDT:USDT:1h")
    await bus.publish("candle:BTC/USDT:USDT:1h", {"close": 69658.6})
    msg = await q.get()
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from loguru import logger


class EventBus:
    """One-to-many pubsub. Subscribers get their own asyncio.Queue."""

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._subs: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()

    def subscribe(self, topic: str) -> asyncio.Queue[Any]:
        """Returns a queue this caller owns. Drop the reference to unsubscribe
        (the queue is garbage-collected on next prune)."""
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subs[topic].append(q)
        return q

    def unsubscribe(self, topic: str, queue: asyncio.Queue[Any]) -> None:
        if queue in self._subs.get(topic, []):
            self._subs[topic].remove(queue)

    async def publish(self, topic: str, message: Any) -> int:
        """Fan out `message` to every subscriber on `topic`. Returns the number
        of subscribers that received it. Full queues drop and log a warning."""
        delivered = 0
        for q in list(self._subs.get(topic, [])):
            try:
                q.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning("event dropped (queue full): topic={}", topic)
        return delivered

    def topics(self) -> list[str]:
        return [t for t, qs in self._subs.items() if qs]


# Process-wide default bus. Modules that don't want to thread it through
# dependency injection can `from cero.events import bus`.
bus = EventBus()
