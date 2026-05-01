"""Compact one-line summaries of GitHub events.

Used by the `inspect` diagnostic script and the `/last` Telegram command.
The per-event-type rendering mirrors the stripped GitHub `/events` payload
shape that Wire stores in `events.payload.raw_payload`.
"""

from __future__ import annotations

from wire.db.models import Event


def format_event_message(e: Event) -> str:
    """Return a short, human-readable message describing one event."""
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
