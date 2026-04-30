"""Wire entrypoint.

Single asyncio event loop. APScheduler runs ingestion / triage / session
detection / drafting / metrics / digest / voice regeneration on cron schedules.
python-telegram-bot runs its own polling loop in the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from wire import __version__
from wire.config import ConfigError, WireConfig, load_config, load_repos
from wire.db import session as db_session
from wire.drafting.drafter import draft_pending_sessions
from wire.health import set_last_ingestion_at, set_queue_size, start_health_server
from wire.ingestion.poller import ingest_all
from wire.ingestion.triage import triage_pending_events
from wire.llm.alerts import evaluate_alert
from wire.llm.provider import build_provider
from wire.metrics.fetcher import fetch_recent_metrics
from wire.sessions.detector import close_idle_sessions, detector_config_from
from wire.telegram import bot as tgbot
from wire.telegram import handlers as tg_handlers
from wire.voice.profile_generator import regenerate_voice_profile

log = structlog.get_logger()


# ---------------- logging ----------------------------------------------------


def configure_logging() -> None:
    level_name = os.environ.get("WIRE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


# ---------------- config -----------------------------------------------------


def load_validated_config(path: Path) -> WireConfig | None:
    if not path.exists():
        if os.environ.get("WIRE_DEV") == "1":
            log.warning(
                "wire.config.missing_dev_mode",
                path=str(path),
                hint="continuing without config (WIRE_DEV=1)",
            )
            return None
        log.error("wire.config.missing", path=str(path))
        raise ConfigError(
            f"Config file not found: {path}. "
            f"Copy data/config.yaml.example to {path} and fill in values."
        )
    config = load_config(path)
    repos_path = config.repos.config_path
    repos = load_repos(repos_path)
    log.info(
        "wire.config.loaded",
        path=str(path),
        repos_path=str(repos_path),
        github_org=config.github.org,
        repo_count=len(repos.repos),
        llm_provider=config.llm.provider,
    )
    return config


# ---------------- signal handling -------------------------------------------


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    def _request_stop(signame: str) -> None:
        log.info("wire.signal.received", signal=signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except (NotImplementedError, AttributeError, ValueError):
            try:
                signal.signal(sig, lambda s, _f: _request_stop(signal.Signals(s).name))
            except (OSError, ValueError):
                pass


# ---------------- scheduler jobs --------------------------------------------


class _Jobs:
    """Bag of stateful job callbacks closed over the global config + provider."""

    def __init__(self, cfg: WireConfig, repos, provider, telegram_app, twitter_client) -> None:
        self.cfg = cfg
        self.repos = repos
        self.provider = provider
        self.telegram_app = telegram_app
        self.twitter_client = twitter_client

    async def run_poll_cycle(self) -> None:
        try:
            await ingest_all(self.cfg, self.repos)
        except Exception:
            log.exception("wire.poll.ingest_failed")
            return
        try:
            n = await triage_pending_events(self.provider)
            log.info("wire.poll.triaged", n=n)
        except Exception:
            log.exception("wire.poll.triage_failed")

        # Sessions
        try:
            cfg_d = detector_config_from(self.cfg)
            from wire.sessions.detector import assign_sessions_for_repo

            for r in self.repos.repos:
                assign_sessions_for_repo(r.name, cfg_d)
            close_idle_sessions(cfg_d)
        except Exception:
            log.exception("wire.poll.sessions_failed")

        # Drafting
        try:
            results = await draft_pending_sessions(self.cfg, self.repos, self.provider)
            log.info("wire.poll.drafted", results_n=len(results))
            # Send freshly created drafts to Telegram (those without telegram_message_id)
            await tgbot.send_pending_drafts_after_quiet(self.telegram_app)
        except Exception:
            log.exception("wire.poll.draft_failed")

        # Update health snapshot
        from datetime import datetime

        set_last_ingestion_at(datetime.now(UTC))
        from sqlalchemy import func, select

        from wire.db.models import Draft

        with db_session.session_scope() as sa:
            n = (
                sa.execute(
                    select(func.count(Draft.id)).where(Draft.status == "pending")
                ).scalar_one()
                or 0
            )
        set_queue_size(int(n))

    async def run_alert_check(self) -> None:
        try:
            _status, alert_text = evaluate_alert(self.cfg)
            if alert_text:
                chat_id = self.telegram_app.bot_data["wire_chat_id"]
                await self.telegram_app.bot.send_message(chat_id=chat_id, text=alert_text)
        except Exception:
            log.exception("wire.alert.failed")

    async def run_metrics(self) -> None:
        if self.twitter_client is None:
            return
        try:
            await fetch_recent_metrics(self.twitter_client)
        except Exception:
            log.exception("wire.metrics.failed")

    async def run_digest(self) -> None:
        try:
            from wire.digest.builder import DigestBuilder

            text = await DigestBuilder(self.cfg).build_text()
            chat_id = self.telegram_app.bot_data["wire_chat_id"]
            await self.telegram_app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("wire.digest.failed")

    async def run_voice(self) -> None:
        try:
            await regenerate_voice_profile(self.cfg, self.provider)
        except Exception:
            log.exception("wire.voice.failed")

    async def run_expire_saved(self) -> None:
        try:
            tg_handlers.expire_old_saved_drafts(max_age_hours=24)
        except Exception:
            log.exception("wire.expire.failed")

    async def run_readme_refresh(self) -> None:
        """Weekly: refresh per-repo README cache for the drafting prompt's
        'About this repo' block."""
        from wire.ingestion.github_client import GitHubClient
        from wire.ingestion.readme_fetcher import refresh_all_readmes

        client = GitHubClient.from_files(
            app_id=self.cfg.github.app_id,
            installation_id=self.cfg.github.installation_id,
            private_key_path=self.cfg.github.private_key_path,
            org=self.cfg.github.org,
        )
        try:
            n = await refresh_all_readmes(client, self.repos)
            log.info("wire.readme.refresh_done", refreshed=n)
        except Exception:
            log.exception("wire.readme.refresh_failed")
        finally:
            await client.aclose()


# ---------------- run --------------------------------------------------------


async def run() -> None:
    log.info("wire.starting", version=__version__)

    config_path = Path(os.environ.get("WIRE_CONFIG_PATH", "/data/config.yaml"))
    config = load_validated_config(config_path)

    health_runner = await start_health_server(host="0.0.0.0", port=8080)
    log.info("wire.health.listening", host="0.0.0.0", port=8080)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    if config is None:
        # WIRE_DEV=1 path: just hold the /health server open, do nothing else.
        log.info("wire.ready", mode="dev")
        try:
            await stop_event.wait()
        finally:
            log.info("wire.stopping")
            await health_runner.cleanup()
            log.info("wire.stopped")
        return

    # --- DB
    db_path = Path(os.environ.get("WIRE_DB_PATH", "/data/wire.db"))
    db_session.init(db_path)
    # Run migrations to head on startup
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig

        cfg_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        if cfg_path.exists():
            ac = AlembicConfig(str(cfg_path))
            command.upgrade(ac, "head")
            log.info("wire.db.migrated")
    except Exception:
        log.exception("wire.db.migration_failed")

    repos = load_repos(config.repos.config_path)
    provider = build_provider(config.llm)

    # Optional Twitter (skip if no token yet)
    twitter_client = None
    if config.twitter.access_token_path.exists():
        from wire.twitter.client import TwitterClient

        twitter_client = TwitterClient(
            client_id=os.environ.get(config.twitter.client_id_env, ""),
            client_secret=os.environ.get(config.twitter.client_secret_env, ""),
            token_path=config.twitter.access_token_path,
        )

    # Telegram
    telegram_app = tgbot.build_application(config, repos, twitter_poster=twitter_client)
    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater is not None:
        await telegram_app.updater.start_polling()
    log.info("wire.telegram.started")

    jobs = _Jobs(config, repos, provider, telegram_app, twitter_client)

    # Scheduler
    sched = AsyncIOScheduler(timezone=str(config.quiet_hours.timezone))
    sched.add_job(
        jobs.run_poll_cycle,
        IntervalTrigger(minutes=config.github.poll_interval_minutes),
        id="poll",
        max_instances=1,
        coalesce=True,
        # Fire on boot too, so /status populates within a minute of startup
        # instead of waiting a full poll interval. APScheduler's default
        # next_run is now+interval, which is unhelpful for fresh containers.
        next_run_time=datetime.now(),
    )
    sched.add_job(
        jobs.run_alert_check,
        IntervalTrigger(minutes=15),
        id="alerts",
        max_instances=1,
    )
    sched.add_job(
        jobs.run_metrics,
        CronTrigger.from_crontab(
            config.metrics.fetch_cron, timezone=str(config.quiet_hours.timezone)
        ),
        id="metrics",
    )
    sched.add_job(
        jobs.run_digest,
        CronTrigger.from_crontab(config.digest.cron, timezone=str(config.quiet_hours.timezone)),
        id="digest",
    )
    # Voice profile: weekly, Sunday 04:00 local
    sched.add_job(
        jobs.run_voice,
        CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=str(config.quiet_hours.timezone)),
        id="voice",
    )
    # README refresh: weekly, Sunday 03:30 local. 30 min before voice so both
    # are warm by the first Monday digest.
    sched.add_job(
        jobs.run_readme_refresh,
        CronTrigger(
            day_of_week="sun", hour=3, minute=30, timezone=str(config.quiet_hours.timezone)
        ),
        id="readme",
    )
    sched.add_job(jobs.run_expire_saved, IntervalTrigger(hours=1), id="expire_saved")
    sched.start()
    log.info("wire.scheduler.started")

    # Wire the digest builder into Telegram so /digest works.
    from wire.digest.builder import DigestBuilder

    telegram_app.bot_data["wire_digest_builder"] = DigestBuilder(config)

    # Run-once on boot: send any drafts still pending without a telegram message
    try:
        sent = await tgbot.send_pending_drafts_after_quiet(telegram_app)
        if sent:
            log.info("wire.boot.sent_pending_drafts", n=sent)
    except Exception:
        log.exception("wire.boot.send_pending_failed")

    log.info("wire.ready")
    try:
        await stop_event.wait()
    finally:
        log.info("wire.stopping")
        sched.shutdown(wait=False)
        try:
            if telegram_app.updater is not None:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception:
            log.exception("wire.telegram.shutdown_failed")
        if twitter_client is not None:
            await twitter_client.aclose()
        await health_runner.cleanup()
        log.info("wire.stopped")


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        log = structlog.get_logger()
        log.exception("wire.fatal")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
