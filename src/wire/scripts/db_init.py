"""`uv run python -m wire.scripts.db_init` — runs all Alembic migrations to head.

Equivalent to `alembic upgrade head` but doesn't require alembic on PATH and
uses the same DB URL that the app does (via WIRE_DB_PATH env or default).
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

# Ensure we run from the project root where alembic.ini lives.
ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    print("DB up to head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
