"""Budget alerting + drafting pause/resume hooks.

Budget logic from SPEC §12:
  - At 80% of cap → Telegram warning (once per crossing).
  - At 100% of cap → drafting paused; triage continues at minimal cost so the
    queue keeps moving. User can /extend to authorize overrun.

The alert state (last threshold crossed) is stored in `bot_state` so we don't
re-warn on every poll cycle.
"""

from __future__ import annotations

import structlog

from wire.config import WireConfig
from wire.db import session as db_session
from wire.db.models import BotState
from wire.llm.budget import BudgetStatus, compute_status

log = structlog.get_logger()

ALERT_KEY = "budget_alert_pct"


def _get_alert_state() -> float:
    with db_session.session_scope() as sa:
        row = sa.get(BotState, ALERT_KEY)
        if row is None:
            return 0.0
        try:
            return float(row.value)
        except ValueError:
            return 0.0


def _set_alert_state(pct: float) -> None:
    with db_session.session_scope() as sa:
        row = sa.get(BotState, ALERT_KEY)
        if row is None:
            sa.add(BotState(key=ALERT_KEY, value=str(pct)))
        else:
            row.value = str(pct)


def evaluate_alert(cfg: WireConfig) -> tuple[BudgetStatus, str | None]:
    """Returns (status, alert_text). alert_text is None if no new alert
    crossing happened since last evaluation."""
    with db_session.session_scope() as sa:
        s = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
    last = _get_alert_state()

    text: str | None = None
    if s.paused and last < 1.0:
        from wire.telegram.voice import say

        text = say("budget_capped", spend=s.spend_usd, cap=s.cap_usd)
        _set_alert_state(1.0)
    elif s.warning and last < cfg.llm.budget_alert_threshold:
        from wire.telegram.voice import say

        text = say("budget_warn", pct=s.pct * 100, spend=s.spend_usd, cap=s.cap_usd)
        _set_alert_state(cfg.llm.budget_alert_threshold)
    elif not s.paused and not s.warning and last >= cfg.llm.budget_alert_threshold:
        # Budget situation eased (e.g. /extend bumped cap). Reset.
        _set_alert_state(0.0)
    return s, text


def is_drafting_blocked_by_budget(cfg: WireConfig) -> bool:
    """True when current-month spend has hit the cap. Triage callers ignore
    this; only drafting checks it before issuing a Sonnet call."""
    with db_session.session_scope() as sa:
        s = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
    return s.paused
