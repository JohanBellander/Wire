"""Health endpoint and shared HealthState singleton.

Step 1 ships the endpoint with placeholder fields. Later steps update the
singleton: ingestion sets last_ingestion_at; drafting updates queue_size;
budget tracking flips status to "degraded" when paused.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from aiohttp import web

from wire import __version__


@dataclass
class HealthState:
    status: str = "ok"
    last_ingestion_at: str | None = None
    queue_size: int = 0
    version: str = __version__

    def to_dict(self) -> dict:
        return asdict(self)


_state = HealthState()


def get_state() -> HealthState:
    return _state


def set_last_ingestion_at(ts: datetime) -> None:
    _state.last_ingestion_at = ts.isoformat()


def set_queue_size(n: int) -> None:
    _state.queue_size = n


def set_status(status: str) -> None:
    _state.status = status


async def _health_handler(_request: web.Request) -> web.Response:
    return web.json_response(_state.to_dict())


async def start_health_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start the /health server. Returns the AppRunner so callers can clean up."""
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner
