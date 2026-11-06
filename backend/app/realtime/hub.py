"""A small, string-channel-keyed WebSocket hub: ``publish(channel, event)``.

This is the generic counterpart of ``app.services.offer_notifier``, which
is keyed ``(company_id, worker_id)`` for the worker offer-push path built in
G4. That path is proven (``tests/test_ws_dispatch.py``) and left untouched
here; this hub is used only for the new dispatcher-facing company channel
(``company:{company_id}``), so a future multi-instance (Redis pub/sub)
adapter has one seam to implement -- ``publish`` -- without the worker path
needing to change.

``HubConnection`` is structurally identical to ``OfferConnection`` (a
``queue``, an ``enqueue`` method with the same overflow handling), so
``app.routers.ws``'s ``_sender``/``_receiver``/``_heartbeat`` work with
either unchanged.
"""

import asyncio
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)

OVERFLOW = object()


@dataclass(eq=False)
class HubConnection:
    channel: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def enqueue(self, item: object) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("hub queue full for channel %s; closing socket", self.channel)
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(OVERFLOW)


class Hub(Protocol):
    def connect(self, channel: str) -> HubConnection: ...

    def disconnect(self, connection: HubConnection) -> None: ...

    def publish(self, channel: str, event: object) -> None: ...


class InProcessHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()  # publish runs on threadpool threads
        self._connections: dict[str, set[HubConnection]] = defaultdict(set)

    def connect(self, channel: str) -> HubConnection:
        """Must run on the socket's event loop: it captures the running loop."""
        connection = HubConnection(
            channel, asyncio.get_running_loop(), asyncio.Queue(maxsize=settings.WS_QUEUE_MAX)
        )
        with self._lock:
            self._connections[channel].add(connection)
        return connection

    def disconnect(self, connection: HubConnection) -> None:
        with self._lock:
            connections = self._connections.get(connection.channel)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    del self._connections[connection.channel]

    def publish(self, channel: str, event: object) -> None:
        with self._lock:
            targets = list(self._connections.get(channel, ()))
        for connection in targets:
            try:
                connection.loop.call_soon_threadsafe(connection.enqueue, event)
            except RuntimeError:  # the socket's loop has closed
                self.disconnect(connection)


_hub: Hub = InProcessHub()


def get_hub() -> Hub:
    return _hub
