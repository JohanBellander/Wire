"""Pytest config for Wire.

asyncio_mode = "auto" is set in pyproject.toml's [tool.pytest.ini_options],
so any `async def test_*` is auto-marked. Shared fixtures will live here as
the suite grows.
"""
