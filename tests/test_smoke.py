"""Step 1 smoke tests: package import + /health endpoint shape."""

from __future__ import annotations

import socket

import aiohttp


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def test_version_exposed():
    from wire import __version__

    assert isinstance(__version__, str)
    assert __version__ == "0.1.0"


async def test_health_endpoint_returns_ok():
    from wire.health import start_health_server

    port = _free_port()
    runner = await start_health_server(host="127.0.0.1", port=port)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
                payload = await resp.json()
    finally:
        await runner.cleanup()

    assert payload["status"] == "ok"
    assert payload["queue_size"] == 0
    assert payload["last_ingestion_at"] is None
    assert payload["version"] == "0.1.0"
