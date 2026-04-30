"""Print Wire's runtime state: bot_state flags + per-repo poll watermarks.

Usage inside the container:
    python -m wire.scripts.state

Read-only; never writes to the DB. Useful for diagnosing:
  - "did the watermark advance?"
  - "is drafting paused?"
  - "what's the budget alert state?"
  - "which repos has the first-run flag flipped on?"
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import desc, func, select

from wire.db import session as db_session
from wire.db.models import BotState, Event
from wire.ingestion.poller import _FIRST_RUN_KEY_PREFIX, _LAST_FETCHED_KEY_PREFIX


def _categorize(key: str) -> str:
    if key.startswith(_FIRST_RUN_KEY_PREFIX):
        return "first-run flags"
    if key.startswith(_LAST_FETCHED_KEY_PREFIX):
        return "fetch watermarks"
    if key.startswith("readme:"):
        return "readme caches"
    return "runtime flags"


def _short_value(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) > 60:
        return value[:57] + "..."
    return value


def main() -> int:
    db_path = os.environ.get("WIRE_DB_PATH", "/data/wire.db")
    db_session.init(db_path)

    with db_session.session_scope() as s:
        bot_state_rows = list(s.execute(select(BotState).order_by(BotState.key)).scalars())

        # Group by category for readability
        groups: dict[str, list[BotState]] = {}
        for row in bot_state_rows:
            groups.setdefault(_categorize(row.key), []).append(row)

        print("=== bot_state ===")
        if not bot_state_rows:
            print("  (empty)")
        for category in ("runtime flags", "first-run flags", "fetch watermarks", "readme caches"):
            rows = groups.get(category, [])
            if not rows:
                continue
            print(f"\n  [{category}]")
            for row in rows:
                # README cache values are too long to print; show the size instead.
                if category == "readme caches":
                    print(f"    {row.key:42s} {len(row.value)} chars")
                else:
                    print(f"    {row.key:42s} {_short_value(row.value)}")
                print(f"    {'':42s}   updated_at={row.updated_at.isoformat()}")

        print("\n=== per-repo poll state ===")
        # All repos that have ever been seen — pull from events + bot_state
        repos_from_events = set(s.execute(select(Event.repo).distinct()).scalars())
        repos_from_state = {
            row.key.removeprefix(_FIRST_RUN_KEY_PREFIX)
            for row in bot_state_rows
            if row.key.startswith(_FIRST_RUN_KEY_PREFIX)
        } | {
            row.key.removeprefix(_LAST_FETCHED_KEY_PREFIX)
            for row in bot_state_rows
            if row.key.startswith(_LAST_FETCHED_KEY_PREFIX)
        }
        repos = sorted(repos_from_events | repos_from_state)

        if not repos:
            print("  (no repos seen yet)")
            return 0

        watermark_by_repo = {
            row.key.removeprefix(_LAST_FETCHED_KEY_PREFIX): row.value
            for row in bot_state_rows
            if row.key.startswith(_LAST_FETCHED_KEY_PREFIX)
        }
        first_run_done = {
            row.key.removeprefix(_FIRST_RUN_KEY_PREFIX)
            for row in bot_state_rows
            if row.key.startswith(_FIRST_RUN_KEY_PREFIX)
        }

        header = (
            f"  {'repo':30s} {'events':>7s}  {'max(occurred_at)':22s}  "
            f"{'watermark':22s}  first_run_done"
        )
        print(header)
        rule = f"  {'-' * 30:30s} {'-' * 7:>7s}  {'-' * 22:22s}  {'-' * 22:22s}  {'-' * 14}"
        print(rule)

        for repo in repos:
            n = s.execute(select(func.count(Event.id)).where(Event.repo == repo)).scalar_one() or 0
            max_occ = s.execute(
                select(Event.occurred_at)
                .where(Event.repo == repo)
                .order_by(desc(Event.occurred_at))
                .limit(1)
            ).scalar_one_or_none()
            wm = watermark_by_repo.get(repo, "(none)")
            done = "yes" if repo in first_run_done else "no"
            max_str = max_occ.isoformat() if max_occ else "(none)"
            print(f"  {repo:30s} {n:>7d}  {max_str:22s}  {wm[:22]:22s}  {done}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
