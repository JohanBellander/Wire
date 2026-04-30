"""Cost estimation + monthly budget tracking.

Cost estimates are rate-table based; rates are best-effort April 2026 numbers
and may drift. Update PRICES if Anthropic changes them. Ollama is $0.

Budget logic (SPEC §12):
  * 80%  → Telegram warn
  * 100% → drafting paused; triage continues
  * /extend [usd] (default +$5) raises the cap for the current calendar month.

The pause/warn helpers in this module are pure — they read llm_calls /
budget_overrides and return a decision. Telegram-side wiring lives in step 11.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session as SASession

# Rate table: USD per 1M tokens.
# Keys are the model strings used in config.yaml (claude.* fields).
# Anthropic cache_read is roughly 10% of input rate, cache_write ~25% over input.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.00,
        "cache_read": 0.08,
        "cache_write": 1.00,
    },
}

# Older / newer model id fallbacks: if we don't recognize the exact string,
# match a family prefix.
_FAMILY_FALLBACKS = (
    ("claude-sonnet", "claude-sonnet-4-6"),
    ("claude-haiku", "claude-haiku-4-5"),
)


def _resolve_rates(model: str) -> dict[str, float] | None:
    if model in PRICES:
        return PRICES[model]
    for prefix, fallback in _FAMILY_FALLBACKS:
        if model.startswith(prefix):
            return PRICES[fallback]
    return None


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return USD cost for one Claude call. Returns 0.0 for unknown models
    (e.g. Ollama) so callers don't need a special case."""
    rates = _resolve_rates(model)
    if rates is None:
        return 0.0
    # input_tokens already includes cache_read + cache_write per Anthropic's
    # billing semantics; subtract them to get the "fresh input" portion.
    fresh_input = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
    return (
        fresh_input * rates["input"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["cache_write"]
        + output_tokens * rates["output"]
    ) / 1_000_000


def current_month_key(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[first-of-month, first-of-next-month) in UTC, naive (matches DB)."""
    month_start = datetime(now.year, now.month, 1)
    if now.month == 12:
        next_start = datetime(now.year + 1, 1, 1)
    else:
        next_start = datetime(now.year, now.month + 1, 1)
    return month_start, next_start


@dataclass
class BudgetStatus:
    spend_usd: float
    cap_usd: float
    extension_usd: float
    pct: float  # 0.0 .. 1.0+
    paused: bool
    warning: bool
    month: str

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spend_usd)


def compute_status(
    session: SASession,
    base_cap_usd: float,
    alert_threshold: float,
    *,
    now: datetime | None = None,
) -> BudgetStatus:
    """Compute current-month spend vs. cap (cap = base + sum of extensions).

    Reads:
      * llm_calls.cost_usd (sum within month)
      * budget_overrides.amount_usd (sum where effective_month == this month)
    """
    from wire.db.models import BudgetOverride, LLMCall

    if now is None:
        now = datetime.now(timezone.utc)
    month_start, next_start = _month_bounds(now.replace(tzinfo=None) if now.tzinfo else now)
    month_key = current_month_key(now)

    spend = session.execute(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
        .where(LLMCall.called_at >= month_start)
        .where(LLMCall.called_at < next_start)
    ).scalar_one()
    extension = session.execute(
        select(func.coalesce(func.sum(BudgetOverride.amount_usd), 0.0))
        .where(BudgetOverride.effective_month == month_key)
    ).scalar_one()

    cap = float(base_cap_usd) + float(extension)
    spend_f = float(spend or 0.0)
    pct = spend_f / cap if cap > 0 else 0.0
    return BudgetStatus(
        spend_usd=spend_f,
        cap_usd=cap,
        extension_usd=float(extension),
        pct=pct,
        paused=pct >= 1.0,
        warning=pct >= alert_threshold and pct < 1.0,
        month=month_key,
    )


def record_extension(
    session: SASession,
    amount_usd: float,
    reason: str | None = None,
    *,
    now: datetime | None = None,
) -> None:
    from wire.db.models import BudgetOverride

    session.add(
        BudgetOverride(
            effective_month=current_month_key(now),
            amount_usd=float(amount_usd),
            reason=reason,
        )
    )
