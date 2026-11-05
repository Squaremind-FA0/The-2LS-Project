"""Push offer events to connected workers.

``OfferNotifier`` is the seam a multi-instance adapter (Redis pub/sub, not
built) implements. ``InProcessOfferNotifier`` only reaches sockets on this
process. ``publish`` is called from sync code on threadpool threads; each
connection's queue is only touched on its own event loop, hence
``call_soon_threadsafe``.
"""

import asyncio
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from app.config import settings
from app.schemas.jobs import OfferEvent

logger = logging.getLogger(__name__)

# Queued instead of an event when a connection overflows: tell the sender to
# close so the client reconnects and re-reads GET /api/offers/pending.
OVERFLOW = object()


@dataclass(eq=False)
class OfferConnection:
    company_id: int
    user_id: int
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def enqueue(self, item: object) -> None:
        """Runs on ``loop``. A bare ``queue.put_nowait`` here would lose
        QueueFull to the loop's exception handler, and the client would never
        know it missed an event."""
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("offer queue full for user %s; closing socket", self.user_id)
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(OVERFLOW)


class OfferNotifier(Protocol):
    def connect(self, company_id: int, user_id: int) -> OfferConnection: ...

    def disconnect(self, connection: OfferConnection) -> None: ...

    def publish(self, company_id: int, worker_id: int, event: OfferEvent) -> None: ...


class InProcessOfferNotifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()  # publish runs on threadpool threads
        self._connections: dict[tuple[int, int], set[OfferConnection]] = defaultdict(set)

    def connect(self, company_id: int, user_id: int) -> OfferConnection:
        """Must run on the socket's event loop: it captures the running loop."""
        connection = OfferConnection(
            company_id,
            user_id,
            asyncio.get_running_loop(),
            asyncio.Queue(maxsize=settings.WS_QUEUE_MAX),
        )
        with self._lock:
            self._connections[(company_id, user_id)].add(connection)
        return connection

    def disconnect(self, connection: OfferConnection) -> None:
        key = (connection.company_id, connection.user_id)
        with self._lock:
            connections = self._connections.get(key)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    del self._connections[key]

    def publish(self, company_id: int, worker_id: int, event: OfferEvent) -> None:
        with self._lock:
            targets = list(self._connections.get((company_id, worker_id), ()))
        for connection in targets:
            try:
                connection.loop.call_soon_threadsafe(connection.enqueue, event)
            except RuntimeError:  # the socket's loop has closed
                self.disconnect(connection)


_notifier: OfferNotifier = InProcessOfferNotifier()


def get_offer_notifier() -> OfferNotifier:
    return _notifier
