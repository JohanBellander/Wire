"""Step 2 — config validation tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from wire.config import (
    ConfigError,
    QuietHoursConfig,
    ReposFile,
    WireConfig,
    load_config,
    load_repos,
)

VALID_CONFIG = textwrap.dedent("""
github:
  org: test-org
  app_id: 123456
  installation_id: 789012
  private_key_path: /data/secrets/github-app.pem
  poll_interval_minutes: 20
repos:
  config_path: /data/repos.yaml
llm:
  provider: claude
  ollama:
    base_url: http://192.168.1.50:11434/
    model: qwen2.5:7b-instruct
    timeout_seconds: 90
  claude:
    drafting: claude-sonnet-4-6
    triage: claude-haiku-4-5
    voice_profile: claude-haiku-4-5
    digest: claude-haiku-4-5
  prompt_caching: true
  monthly_budget_usd: 10
  budget_alert_threshold: 0.8
session:
  idle_minutes: 30
  max_hours: 4
  immediate_trigger_events:
    - release
    - milestone
quiet_hours:
  start: "22:00"
  end: "07:00"
  timezone: "Europe/Stockholm"
telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID
twitter:
  client_id_env: TWITTER_CLIENT_ID
  client_secret_env: TWITTER_CLIENT_SECRET
  access_token_path: /data/secrets/twitter-token.json
metrics:
  fetch_cron: "0 9 * * *"
  posts_settle_days: 7
digest:
  cron: "0 9 * * 1"
learning:
  recent_decisions_n: 20
  recent_posts_n: 30
logging:
  level: INFO
  format: json
""").strip()


VALID_REPOS = textwrap.dedent("""
repos:
  - name: winetrackr
    visibility: public
    notes: "Public side project"
  - name: home-server-config
    visibility: private
    notes: "Personal infra"
""").strip()


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_config_loads(tmp_path):
    p = _write(tmp_path, "config.yaml", VALID_CONFIG)
    cfg = load_config(p)
    assert isinstance(cfg, WireConfig)
    assert cfg.github.org == "test-org"
    assert cfg.llm.provider == "claude"
    assert cfg.llm.claude.drafting == "claude-sonnet-4-6"
    assert cfg.session.idle_minutes == 30
    assert "release" in cfg.session.immediate_trigger_events
    # default ingestion config picked up
    assert any("chore" in p for p in cfg.ingestion.skip_commit_patterns)


def test_ollama_base_url_trailing_slash_stripped(tmp_path):
    p = _write(tmp_path, "config.yaml", VALID_CONFIG)
    cfg = load_config(p)
    assert cfg.llm.ollama.base_url == "http://192.168.1.50:11434"


def test_invalid_provider_rejected(tmp_path):
    bad = VALID_CONFIG.replace("provider: claude", "provider: groq")
    p = _write(tmp_path, "config.yaml", bad)
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_threshold_range(tmp_path):
    bad = VALID_CONFIG.replace("budget_alert_threshold: 0.8", "budget_alert_threshold: 1.5")
    p = _write(tmp_path, "config.yaml", bad)
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_required_field(tmp_path):
    bad = VALID_CONFIG.replace("  org: test-org\n", "")
    p = _write(tmp_path, "config.yaml", bad)
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_timezone_rejected(tmp_path):
    bad = VALID_CONFIG.replace('timezone: "Europe/Stockholm"', 'timezone: "Mars/Olympus"')
    p = _write(tmp_path, "config.yaml", bad)
    with pytest.raises(ConfigError):
        load_config(p)


def test_quiet_hours_tzinfo_works():
    qh = QuietHoursConfig(start="22:00", end="07:00", timezone="Europe/Stockholm")
    info = qh.tzinfo
    assert info.key == "Europe/Stockholm"


def test_negative_poll_interval(tmp_path):
    bad = VALID_CONFIG.replace("poll_interval_minutes: 20", "poll_interval_minutes: -5")
    p = _write(tmp_path, "config.yaml", bad)
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_file_fails_fast(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_empty_file_fails(tmp_path):
    p = _write(tmp_path, "config.yaml", "")
    with pytest.raises(ConfigError):
        load_config(p)


def test_top_level_not_mapping(tmp_path):
    p = _write(tmp_path, "config.yaml", "- just\n- a list")
    with pytest.raises(ConfigError):
        load_config(p)


# ---------------- repos.yaml ----------------


def test_valid_repos_loads(tmp_path):
    p = _write(tmp_path, "repos.yaml", VALID_REPOS)
    rf = load_repos(p)
    assert isinstance(rf, ReposFile)
    assert rf.names() == {"winetrackr", "home-server-config"}
    assert rf.get("winetrackr").visibility == "public"


def test_repos_duplicate_names_rejected(tmp_path):
    duplicated = textwrap.dedent("""
    repos:
      - name: winetrackr
        visibility: public
        notes: "first"
      - name: winetrackr
        visibility: private
        notes: "duplicate"
    """).strip()
    p = _write(tmp_path, "repos.yaml", duplicated)
    with pytest.raises(ConfigError):
        load_repos(p)


def test_repos_invalid_visibility_rejected(tmp_path):
    bad = textwrap.dedent("""
    repos:
      - name: winetrackr
        visibility: secret
        notes: "?"
    """).strip()
    p = _write(tmp_path, "repos.yaml", bad)
    with pytest.raises(ConfigError):
        load_repos(p)


def test_repos_slash_in_name_rejected(tmp_path):
    bad = textwrap.dedent("""
    repos:
      - name: visma-org/winetrackr
        visibility: public
        notes: ""
    """).strip()
    p = _write(tmp_path, "repos.yaml", bad)
    with pytest.raises(ConfigError):
        load_repos(p)
