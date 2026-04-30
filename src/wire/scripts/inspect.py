"""Diagnostic — print a summary of what Wire has done in the last N hours.

Usage inside the container:
    python -m wire.scripts.inspect            # default: last 24h
    python -m wire.scripts.inspect 10         # last 10h
    python -m wire.scripts.inspect 48 --top 25  # last 48h, show top 25 events

Goes against /data/wire.db (or whatever WIRE_DB_PATH points at). Read-only.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, select

from wire.db import session as db_session
from wire.db.models import Decision, Draft, Event, LLMCall, Post, Session


def _format_event_message(e: Event) -> str:
    raw = (e.payload or {}).get("raw_payload", {})
    if e.event_type == "PushEvent":
        commits = raw.get("commits") or []
        if commits:
            msg = commits[0].get("message", "").splitlines()[0]
            extra = f" (+{len(commits) - 1} more)" if len(commits) > 1 else ""
            return f"{msg[:80]}{extra}"
        return "(no commits)"
    if e.event_type == "PullRequestEvent":
        pr = raw.get("pull_request") or {}
        title = (pr.get("title") or "(no title)")[:80]
        return (
            f'#{pr.get("number", "?")} "{title}" '
            f"action={raw.get('action')} merged={pr.get('merged')}"
        )
    if e.event_type == "ReleaseEvent":
        rel = raw.get("release") or {}
        return f'{rel.get("tag_name", "")} "{(rel.get("name") or "")[:60]}"'
    if e.event_type == "IssuesEvent":
        issue = raw.get("issue") or {}
        return f'"{(issue.get("title") or "")[:80]}" action={raw.get("action")}'
    if e.event_type == "CreateEvent":
        return f'{raw.get("ref_type", "?")} "{raw.get("ref", "?")}"'
    if e.event_type == "DeleteEvent":
        return f'{raw.get("ref_type", "?")} "{raw.get("ref", "?")}"'
    if e.event_type == "IssueCommentEvent":
        issue = raw.get("issue") or {}
        return f'comment on "{(issue.get("title") or "")[:60]}"'
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "hours", nargs="?", type=int, default=24, help="Window length in hours (default 24)"
    )
    parser.add_argument(
        "--top", type=int, default=15, help="Show this many top-scored events (default 15)"
    )
    args = parser.parse_args()

    db_path = os.environ.get("WIRE_DB_PATH", "/data/wire.db")
    db_session.init(db_path)
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=args.hours)

    print(f"=== Wire activity, last {args.hours}h (since {cutoff.isoformat()} UTC) ===\n")

    with db_session.session_scope() as s:
        # --- per-repo event counts + avg triage --------------------------------
        rows = s.execute(
            select(
                Event.repo,
                func.count(Event.id),
                func.avg(Event.triage_score),
                func.max(Event.triage_score),
            )
            .where(Event.ingested_at >= cutoff)
            .group_by(Event.repo)
            .order_by(desc(func.count(Event.id)))
        ).all()
        print("Events ingested per repo:")
        if not rows:
            print("  (none)")
        for repo, n, avg, mx in rows:
            avg_s = f"{avg:.2f}" if avg is not None else " ?  "
            mx_s = f"{mx:.2f}" if mx is not None else " ?  "
            print(f"  {repo:28s} {n:4d} events   avg={avg_s}  max={mx_s}")
        print()

        # --- triage distribution ----------------------------------------------
        buckets = [(0.0, 0.3, "boring"), (0.3, 0.6, "maybe"), (0.6, 1.01, "interesting")]
        print("Triage distribution:")
        for lo, hi, label in buckets:
            n = s.execute(
                select(func.count(Event.id))
                .where(Event.ingested_at >= cutoff)
                .where(Event.triage_score >= lo)
                .where(Event.triage_score < hi)
            ).scalar_one()
            print(f"  [{lo:.1f} – {hi:.2f}) {label:12s} {n}")
        unscored = s.execute(
            select(func.count(Event.id))
            .where(Event.ingested_at >= cutoff)
            .where(Event.triage_score.is_(None))
        ).scalar_one()
        if unscored:
            print(f"  unscored (triage failed)         {unscored}")
        print()

        # --- top-scored events ------------------------------------------------
        print(f"Top {args.top} highest-scored events (Wire found these worth considering):")
        events = (
            s.execute(
                select(Event)
                .where(Event.ingested_at >= cutoff)
                .where(Event.triage_score.is_not(None))
                .order_by(desc(Event.triage_score))
                .limit(args.top)
            )
            .scalars()
            .all()
        )
        if not events:
            print("  (none)")
        for e in events:
            score = f"{e.triage_score:.2f}"
            msg = _format_event_message(e)
            print(f"  [{score}] {e.repo:22s} {e.event_type:18s} {msg}")
            if e.triage_reason:
                print(f'         reason: "{e.triage_reason}"')
        print()

        # --- sessions --------------------------------------------------------
        print("Sessions formed:")
        rows = s.execute(
            select(Session.repo, Session.closed_reason, func.count(Session.id))
            .where(Session.started_at >= cutoff)
            .group_by(Session.repo, Session.closed_reason)
            .order_by(Session.repo)
        ).all()
        if not rows:
            print("  (none)")
        for repo, reason, n in rows:
            print(f"  {repo:25s} {n:3d}x   closed={reason or 'open'}")
        print()

        # --- drafts ----------------------------------------------------------
        print("Drafts:")
        rows = s.execute(
            select(Draft.status, func.count(Draft.id))
            .where(Draft.created_at >= cutoff)
            .group_by(Draft.status)
        ).all()
        if not rows:
            print("  (none)")
        for status, n in rows:
            print(f"  {status:10s} {n}")
        print()

        # --- recent draft texts (sample) ------------------------------------
        recent_drafts = (
            s.execute(
                select(Draft)
                .where(Draft.created_at >= cutoff)
                .order_by(desc(Draft.created_at))
                .limit(5)
            )
            .scalars()
            .all()
        )
        if recent_drafts:
            print("Recent drafts (most recent first):")
            for d in recent_drafts:
                snip = (d.text or "").replace("\n", " ")[:120]
                print(f"  #{d.id} [{d.status}] {snip}")
                if d.reasoning:
                    print(f"      reasoning: {d.reasoning[:120]}")
            print()

        # --- decisions made --------------------------------------------------
        decisions = s.execute(
            select(Decision.decision, Decision.reject_reason, func.count(Decision.id))
            .where(Decision.decided_at >= cutoff)
            .group_by(Decision.decision, Decision.reject_reason)
        ).all()
        if decisions:
            print("Your decisions:")
            for kind, reason, n in decisions:
                tail = f" ({reason})" if reason else ""
                print(f"  {kind}{tail}: {n}")
            print()

        # --- posts to X ------------------------------------------------------
        n_posts = s.execute(
            select(func.count(Post.id)).where(Post.posted_at >= cutoff)
        ).scalar_one()
        print(f"Posts to X: {n_posts}")
        print()

        # --- LLM activity ----------------------------------------------------
        print("LLM activity:")
        rows = s.execute(
            select(
                LLMCall.task,
                LLMCall.provider,
                LLMCall.model,
                func.count(LLMCall.id),
                func.sum(LLMCall.cost_usd),
                func.avg(LLMCall.fallback.cast(__import__("sqlalchemy").Float)),
                func.avg(LLMCall.latency_ms),
            )
            .where(LLMCall.called_at >= cutoff)
            .group_by(LLMCall.task, LLMCall.provider, LLMCall.model)
            .order_by(desc(func.count(LLMCall.id)))
        ).all()
        if not rows:
            print("  (none)")
        total_cost = 0.0
        for task, provider, model, n, cost, fb_rate, latency in rows:
            cost = cost or 0.0
            total_cost += cost
            fb_pct = (fb_rate or 0) * 100
            print(
                f"  {task:14s} via {provider:7s} {model or '-':22s} "
                f"{n:4d}x  ${cost:.4f}  fallback={fb_pct:.0f}%  avg={int(latency or 0)}ms"
            )
        print(f"  {'TOTAL':14s}                                           ${total_cost:.4f}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
