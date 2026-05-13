"""Weekly digest builder. Per SPEC §7.6.

Compose a single Telegram message with last-7-days stats: drafted, posted,
rejected, saved counts; approval rate; top performers and below-median posts;
LLM spend + fallback rate. Times are converted to Europe/Stockholm at the
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import desc, func, select

from wire.config import WireConfig
from wire.db import session as db_session
from wire.db.models import Decision, Draft, LLMCall, Metric, Post, utc_now
from wire.llm.budget import compute_status

log = structlog.get_logger()


@dataclass
class DigestNumbers:
    drafted: int
    posted: int
    rejected: int
    saved_pending: int
    approval_rate_pct: float
    top: list[tuple[Post, Metric | None]]
    below_median: list[tuple[Post, Metric | None]]
    spend_usd: float
    cap_usd: float
    fallback_rate_pct: float
    primary_provider: str


def _last_metric_for(sa, post_id: int) -> Metric | None:
    return sa.execute(
        select(Metric).where(Metric.post_id == post_id).order_by(desc(Metric.fetched_at)).limit(1)
    ).scalar_one_or_none()


def gather_numbers(cfg: WireConfig, *, now: datetime | None = None) -> DigestNumbers:
    if now is None:
        now = utc_now()
    week_start = now - timedelta(days=7)
    settle_cutoff = now - timedelta(days=cfg.metrics.posts_settle_days)

    with db_session.session_scope() as sa:
        # last 7 days of activity
        drafted = sa.execute(
            select(func.count(Draft.id)).where(Draft.created_at >= week_start)
        ).scalar_one()
        posted = sa.execute(
            select(func.count(Post.id)).where(Post.posted_at >= week_start)
        ).scalar_one()
        rejected = sa.execute(
            select(func.count(Decision.id))
            .where(Decision.decided_at >= week_start)
            .where(Decision.decision == "rejected")
        ).scalar_one()
        saved_pending = sa.execute(
            select(func.count(Draft.id)).where(Draft.status == "pending")
        ).scalar_one()
        approved = sa.execute(
            select(func.count(Decision.id))
            .where(Decision.decided_at >= week_start)
            .where(Decision.decision.in_(("approved", "edited")))
        ).scalar_one()
        decided_total = approved + rejected
        approval = (approved / decided_total) if decided_total else 0.0

        # top + bottom performers among settled posts
        settled_posts = (
            sa.execute(
                select(Post)
                .where(Post.posted_at <= settle_cutoff)
                .order_by(desc(Post.posted_at))
                .limit(60)
            )
            .scalars()
            .all()
        )
        with_metric = [(p, _last_metric_for(sa, p.id)) for p in settled_posts]
        with_metric_filtered = [t for t in with_metric if t[1] is not None]
        with_metric_filtered.sort(key=lambda t: t[1].impressions or 0, reverse=True)

        top = with_metric_filtered[:3]
        impressions_sorted = sorted(t[1].impressions or 0 for t in with_metric_filtered)
        median = impressions_sorted[len(impressions_sorted) // 2] if impressions_sorted else 0
        below = [t for t in with_metric_filtered if (t[1].impressions or 0) < median][:3]

        # LLM spend + fallback
        spend = compute_status(sa, cfg.llm.monthly_budget_usd, cfg.llm.budget_alert_threshold)
        total = sa.execute(
            select(func.count(LLMCall.id)).where(LLMCall.called_at >= week_start)
        ).scalar_one()
        fb = sa.execute(
            select(func.count(LLMCall.id))
            .where(LLMCall.called_at >= week_start)
            .where(LLMCall.fallback.is_(True))
        ).scalar_one()
        fb_rate = (fb / total) if total else 0.0

    primary = "Claude" if cfg.llm.provider == "claude" else "llama.cpp"
    return DigestNumbers(
        drafted=drafted,
        posted=posted,
        rejected=rejected,
        saved_pending=saved_pending,
        approval_rate_pct=approval * 100,
        top=top,
        below_median=below,
        spend_usd=spend.spend_usd,
        cap_usd=spend.cap_usd,
        fallback_rate_pct=fb_rate * 100,
        primary_provider=primary,
    )


def format_digest_body(n: DigestNumbers) -> str:
    """Pure-Python factual stats block. Persona-agnostic — every number
    is preserved verbatim. Header line ('📊 Last 7 days') is intentionally
    NOT included here; it's added by `format_digest()` so the persona
    layer can replace it."""
    lines = [
        f"Drafted: {n.drafted} · Posted: {n.posted} · Rejected: {n.rejected} · "
        f"Saved: {n.saved_pending}",
        f"Approval rate: {n.approval_rate_pct:.0f}%",
        "",
    ]
    if n.top:
        lines.append("Top performers (≥7-day-settled):")
        for i, (p, m) in enumerate(n.top, 1):
            impr = m.impressions if m else None
            likes = m.likes if m else None
            snip = (p.text or "")[:80].replace("\n", " ")
            lines.append(f'{i}. "{snip}…" ({impr or 0} impr, {likes or 0} ❤️)')
        lines.append("")
    if n.below_median:
        lines.append("Below median:")
        for i, (p, m) in enumerate(n.below_median, 1):
            impr = m.impressions if m else None
            likes = m.likes if m else None
            snip = (p.text or "")[:80].replace("\n", " ")
            lines.append(f'{i}. "{snip}…" ({impr or 0} impr, {likes or 0} ❤️)')
        lines.append("")
    spend_pct = (n.spend_usd / n.cap_usd * 100) if n.cap_usd else 0.0
    lines.append("LLM usage:")
    lines.append(f"• Spend this month: ${n.spend_usd:.2f} / ${n.cap_usd:.2f} ({spend_pct:.0f}%)")
    lines.append(f"• Provider: {n.primary_provider} · fallback rate: {n.fallback_rate_pct:.0f}%")
    return "\n".join(lines)


def format_digest(
    n: DigestNumbers,
    *,
    opener: str | None = None,
    closer: str | None = None,
) -> str:
    """Compose the full digest message: opener + stats + closer.

    Without persona framing, falls back to the static "📊 Last 7 days" header
    and no closer — same shape as the pre-persona version.
    """
    body = format_digest_body(n)
    head = opener if opener else "📊 Last 7 days"
    tail = f"\n\n{closer}" if closer else ""
    return f"{head}\n\n{body}{tail}"


class DigestBuilder:
    """Bound to a WireConfig; exposes a `build_text()` coroutine for the
    Telegram /digest command and the scheduled cron."""

    def __init__(self, cfg: WireConfig, provider=None) -> None:
        self.cfg = cfg
        self.provider = provider

    async def build_text(self) -> str:
        numbers = gather_numbers(self.cfg)
        body = format_digest_body(numbers)

        opener: str | None = None
        closer: str | None = None
        try:
            from wire.telegram import persona as persona_mod

            framed = await persona_mod.frame_digest(self.cfg, self.provider, stats_block=body)
            if framed is not None:
                opener, closer = framed
        except Exception:  # noqa: BLE001 — persona must never break the digest
            log.exception("wire.digest.persona_failed")

        return format_digest(numbers, opener=opener, closer=closer)


async def send_digest_to_telegram(app, cfg: WireConfig) -> None:
    provider = app.bot_data.get("wire_provider")
    text = await DigestBuilder(cfg, provider=provider).build_text()
    chat_id = app.bot_data["wire_chat_id"]
    await app.bot.send_message(chat_id=chat_id, text=text)
