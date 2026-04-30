"""Session detection. Per SPEC.MD §7.2.

Sessions are per-repo. An event joins an open session if it occurred within
`idle_minutes` of the previous event in that session. A session closes when:
  (i) idle_minutes have passed since the last event, OR
  (ii) duration exceeds max_hours, OR
  (iii) it contains an immediate-trigger event type (release, milestone) —
       which forces an immediate close regardless of idle time.

The detector runs on every poll cycle. It assigns sessions to ungrouped events
in order, and closes sessions whose tail is far enough in the past.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import asc, select

from wire.db import session as db_session
from wire.db.models import Event, Session, utc_now

log = structlog.get_logger()

# Map raw GitHub event types to the immediate-trigger keys used in
# config.session.immediate_trigger_events.
_IMMEDIATE_KEY_FOR: dict[str, str] = {
    "ReleaseEvent": "release",
    "MilestoneEvent": "milestone",
    # PullRequestEvent with action=closed+merged could be a milestone too —
    # not currently mapped; SPEC defaults are release/milestone.
}


def _trigger_key(event: Event) -> str | None:
    return _IMMEDIATE_KEY_FOR.get(event.event_type)


@dataclass
class DetectorConfig:
    idle_minutes: int
    max_hours: int
    immediate_trigger_events: frozenset[str]


def _open_session_for(repo: str, sa_session) -> Session | None:
    """Return the most recent still-open session for a repo, or None."""
    return sa_session.execute(
        select(Session)
        .where(Session.repo == repo)
        .where(Session.ended_at.is_(None))
        .order_by(Session.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _ungrouped_events(repo: str, sa_session) -> list[Event]:
    return list(
        sa_session.execute(
            select(Event)
            .where(Event.repo == repo)
            .where(Event.session_id.is_(None))
            .order_by(asc(Event.occurred_at))
        ).scalars()
    )


def _close_session(s: Session, when: datetime, reason: str) -> None:
    s.ended_at = when
    s.closed_reason = reason


def _should_join(
    open_session: Session, event: Event, cfg: DetectorConfig, last_in_session_at: datetime
) -> bool:
    """Decide whether `event` joins `open_session`. Only the idle gap matters
    here — the max_hours boundary is handled *after* admitting the event so
    the closing reason is recorded as 'max_hours' on the session that just
    overflowed (per SPEC §7.2: 'session duration exceeds max_hours')."""
    gap = event.occurred_at - last_in_session_at
    return gap <= timedelta(minutes=cfg.idle_minutes)


def assign_sessions_for_repo(repo: str, cfg: DetectorConfig) -> int:
    """Group all ungrouped events for one repo into sessions. Returns the
    number of events assigned in this pass."""
    assigned = 0
    with db_session.session_scope() as sa:
        events = _ungrouped_events(repo, sa)
        if not events:
            return 0

        open_sess = _open_session_for(repo, sa)
        # Determine the last-event-in-session timestamp if there's an open session.
        last_at: datetime | None = None
        if open_sess is not None:
            last_at = (
                sa.execute(
                    select(Event.occurred_at)
                    .where(Event.session_id == open_sess.id)
                    .order_by(Event.occurred_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                or open_sess.started_at
            )

        for e in events:
            # Immediate trigger forces close-if-open then a fresh single-event session.
            trigger = _trigger_key(e)
            if trigger is not None and trigger in cfg.immediate_trigger_events:
                if open_sess is not None:
                    _close_session(open_sess, last_at or open_sess.started_at, "idle")
                fresh = Session(repo=repo, started_at=e.occurred_at)
                sa.add(fresh)
                sa.flush()
                e.session_id = fresh.id
                _close_session(fresh, e.occurred_at, "immediate")
                open_sess = None
                last_at = None
                assigned += 1
                continue

            # Decide: extend open session or start a new one.
            if (
                open_sess is not None
                and last_at is not None
                and _should_join(open_sess, e, cfg, last_at)
            ):
                e.session_id = open_sess.id
                last_at = e.occurred_at
                # Did we just exceed max_hours? Close after assigning.
                if (e.occurred_at - open_sess.started_at) > timedelta(hours=cfg.max_hours):
                    _close_session(open_sess, e.occurred_at, "max_hours")
                    open_sess = None
                    last_at = None
            else:
                # Close prior open session as idle (its events were too far back).
                if open_sess is not None:
                    _close_session(open_sess, last_at or open_sess.started_at, "idle")
                fresh = Session(repo=repo, started_at=e.occurred_at)
                sa.add(fresh)
                sa.flush()
                e.session_id = fresh.id
                open_sess = fresh
                last_at = e.occurred_at
            assigned += 1

    return assigned


def close_idle_sessions(cfg: DetectorConfig, *, now: datetime | None = None) -> int:
    """Close any still-open session whose last event is older than idle_minutes.
    Also closes max_hours-overflowing sessions if anything slipped past."""
    if now is None:
        now = utc_now()
    closed = 0
    with db_session.session_scope() as sa:
        open_rows: Iterable[Session] = sa.execute(
            select(Session).where(Session.ended_at.is_(None))
        ).scalars()
        for s in list(open_rows):
            last_at = sa.execute(
                select(Event.occurred_at)
                .where(Event.session_id == s.id)
                .order_by(Event.occurred_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            tail = last_at or s.started_at
            if (now - tail) > timedelta(minutes=cfg.idle_minutes):
                _close_session(s, tail, "idle")
                closed += 1
            elif (now - s.started_at) > timedelta(hours=cfg.max_hours):
                _close_session(s, tail, "max_hours")
                closed += 1
    return closed


def detector_config_from(config) -> DetectorConfig:
    return DetectorConfig(
        idle_minutes=config.session.idle_minutes,
        max_hours=config.session.max_hours,
        immediate_trigger_events=frozenset(config.session.immediate_trigger_events),
    )
