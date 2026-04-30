"""Inspect what's actually stored in the events table for PR / Create / Push
events. Diagnoses payload-shape issues like missing PR titles.

Run inside the Wire container:

    CID=$(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q)
    docker exec "$CID" python /tmp/inspect_payload.py

Or pipe straight in via SSH (no file-copy step):

    cat scripts/inspect_payload.py | ssh johan@gary \
        "docker exec -i \$(docker ps -q -f name=j13i32n8rrvzsxpydl404f6v) python /dev/stdin"
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("WIRE_DB_PATH", "/data/wire.db")


def _peek(d: dict, *paths: str) -> str:
    """Return repr of d.path1.path2... or '<missing>'."""
    cur: object = d
    for p in paths:
        if not isinstance(cur, dict) or p not in cur:
            return "<missing>"
        cur = cur[p]
    return repr(cur)[:140]


def _show_event(eid: int, ghid: str, et: str, pl: object) -> None:
    payload = json.loads(pl) if isinstance(pl, str) else pl
    raw = payload.get("raw_payload") or {}

    print(f"--- event id={eid} github_id={ghid} type={et} ---")
    print(f"  payload keys     : {list(payload.keys())}")
    print(f"  raw_payload keys : {list(raw.keys())}")

    if et == "PullRequestEvent":
        pr = raw.get("pull_request") or {}
        print(f"  pr present       : {bool(pr)}")
        print(f"  pr keys          : {sorted(pr.keys())[:25]}")
        print(f"  pr.title         : {_peek(pr, 'title')}")
        print(f"  pr.body length   : {len(pr.get('body') or '')}")
        print(f"  pr.merged        : {_peek(pr, 'merged')}")
        print(f"  pr.state         : {_peek(pr, 'state')}")
        print(f"  pr.number        : {_peek(pr, 'number')}")
        print(f"  pr.html_url      : {_peek(pr, 'html_url')}")
        print(f"  raw.action       : {_peek(raw, 'action')}")
        print(f"  raw.number       : {_peek(raw, 'number')}")
    elif et == "CreateEvent":
        print(f"  raw.ref_type     : {_peek(raw, 'ref_type')}")
        print(f"  raw.ref          : {_peek(raw, 'ref')}")
        print(f"  raw.master_branch: {_peek(raw, 'master_branch')}")
    elif et == "DeleteEvent":
        print(f"  raw.ref_type     : {_peek(raw, 'ref_type')}")
        print(f"  raw.ref          : {_peek(raw, 'ref')}")
    elif et == "PushEvent":
        commits = raw.get("commits") or []
        print(f"  raw.ref          : {_peek(raw, 'ref')}")
        print(f"  commits count    : {len(commits)}")
        for c in commits[:3]:
            print(f"    - {c.get('message', '').splitlines()[0][:80]}")
    print()


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"FATAL: db not found at {DB_PATH}", file=sys.stderr)
        return 2

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    print(f"=== inspecting {DB_PATH} ===\n")

    for et, n in [
        ("PullRequestEvent", 3),
        ("CreateEvent", 2),
        ("DeleteEvent", 2),
        ("PushEvent", 2),
    ]:
        rows = con.execute(
            "SELECT id, github_id, event_type, payload FROM events "
            "WHERE event_type = ? ORDER BY id DESC LIMIT ?",
            (et, n),
        ).fetchall()
        if not rows:
            print(f"--- no {et} rows ---\n")
            continue
        for r in rows:
            _show_event(r["id"], r["github_id"], r["event_type"], r["payload"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
