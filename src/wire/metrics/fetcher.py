"""Daily X engagement metrics fetcher. Per SPEC §7.5.

For all posts < 30 days old, fetch current metrics from the X API and append
one row per fetch to `metrics` (keep history; never overwrite). Posts older
than `posts_settle_days` are eligible for use as performance signal in the
drafting prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from wire.db import session as db_session
from wire.db.models import Metric, Post, utc_now
from wire.twitter.client import TwitterClient

log = structlog.get_logger()


async def fetch_recent_metrics(client: TwitterClient, *, max_age_days: int = 30) -> int:
    """Fetch and persist metrics for posts younger than max_age_days. Returns
    the number of metric rows appended."""
    cutoff = utc_now() - timedelta(days=max_age_days)
    with db_session.session_scope() as sa:
        rows = sa.execute(
            select(Post).where(Post.posted_at >= cutoff)
        ).scalars().all()
        post_index: dict[str, int] = {r.twitter_id: r.id for r in rows}

    if not post_index:
        log.info("wire.metrics.no_recent_posts")
        return 0

    fetched = await client.fetch_metrics(list(post_index.keys()))
    if not fetched:
        log.info("wire.metrics.empty_response", asked=len(post_index))
        return 0

    appended = 0
    with db_session.session_scope() as sa:
        for m in fetched:
            pid = post_index.get(m.tweet_id)
            if pid is None:
                continue
            sa.add(Metric(
                post_id=pid,
                impressions=m.impressions,
                likes=m.likes,
                retweets=m.retweets,
                replies=m.replies,
                bookmarks=m.bookmarks,
            ))
            appended += 1
    log.info("wire.metrics.appended", count=appended)
    return appended
