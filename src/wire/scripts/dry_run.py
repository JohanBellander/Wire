"""`uv run python -m wire.scripts.dry_run` — ingest the last 24h of events
into a temp DB and print kept-vs-filtered.

No Telegram messages, no LLM calls, no posting. Just a quick sanity check
that GitHub auth works and filters do what's expected.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select

from wire.config import load_config, load_repos
from wire.db import session as db_session
from wire.db.models import Base, Event
from wire.ingestion.poller import ingest_all


async def _main() -> int:
    cfg_path = Path(os.environ.get("WIRE_CONFIG_PATH", "/data/config.yaml"))
    config = load_config(cfg_path)
    repos = load_repos(config.repos.config_path)

    tmp_db = Path(tempfile.mkstemp(prefix="wire-dry-", suffix=".db")[1])
    print(f"Using temp DB: {tmp_db}")
    os.environ["WIRE_DB_PATH"] = str(tmp_db)
    db_session.reset_for_tests()
    engine = db_session.init(tmp_db)
    Base.metadata.create_all(engine)

    stats = await ingest_all(config, repos)

    print()
    print("=== Per-repo summary ===")
    for s in stats:
        print(
            f"{s.repo:30s}  fetched={s.fetched:4d}  kept={s.kept:4d}  "
            f"dropped={s.dropped:4d}  inserted={s.inserted:4d}"
        )
        if s.drop_reasons:
            for reason, count in sorted(s.drop_reasons.items()):
                print(f"   - {reason}: {count}")

    print()
    print("=== Kept events (sample) ===")
    with db_session.session_scope() as ss:
        events = ss.execute(
            select(Event).order_by(Event.occurred_at.desc()).limit(20)
        ).scalars().all()
        for e in events:
            print(f"  {e.occurred_at}  {e.repo:25s}  {e.event_type:20s}  by {e.actor}")

    print()
    print("Dry run complete. No Telegram messages sent, no posts made.")
    print(f"Temp DB left at {tmp_db} for inspection (delete when done).")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
