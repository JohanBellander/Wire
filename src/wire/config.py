"""Config loading + pydantic validation.

Two YAML files: /data/config.yaml (the main config) and /data/repos.yaml (the
allowlist). Both are validated with pydantic — any structural error fails the
process at startup with a clear message. See SPEC.MD §4 and §5.
"""

from __future__ import annotations

import os
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class GithubConfig(BaseModel):
    org: str
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    private_key_path: Path
    poll_interval_minutes: int = Field(ge=1, le=240)


class ReposLocation(BaseModel):
    config_path: Path


class LlamaCppConfig(BaseModel):
    """OpenAI-compatible HTTP backend (llama.cpp server, vLLM, OpenRouter, etc.).

    The endpoint must speak OpenAI's `POST /v1/chat/completions` shape.
    `base_url` should include the `/v1` segment (e.g. `https://llm.example.com/v1`).
    Auth is `Authorization: Bearer <api_key>`, where the key is read from the
    env var named in `api_key_env` at boot — set the env var in Coolify or
    `.env`, not here. Empty / missing key is allowed for unauth'd local servers.
    """

    base_url: str
    model: str
    timeout_seconds: int = Field(ge=1, le=600)
    api_key_env: str = "LLM_API_KEY"
    temperature: float = Field(default=0.5, ge=0.0, le=2.0)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class ClaudeModelsConfig(BaseModel):
    drafting: str
    triage: str
    voice_profile: str
    digest: str


class LLMConfig(BaseModel):
    provider: Literal["claude", "llamacpp"]
    llamacpp: LlamaCppConfig | None = None
    claude: ClaudeModelsConfig
    prompt_caching: bool = True
    monthly_budget_usd: float = Field(gt=0)
    budget_alert_threshold: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def _llamacpp_present_when_selected(self) -> LLMConfig:
        if self.provider == "llamacpp" and self.llamacpp is None:
            raise ValueError(
                "WIRE_LLM_PROVIDER=llamacpp requires WIRE_LLAMACPP_BASE_URL "
                "+ WIRE_LLAMACPP_MODEL env vars"
            )
        return self


class SessionConfig(BaseModel):
    idle_minutes: int = Field(gt=0)
    max_hours: int = Field(gt=0)
    immediate_trigger_events: list[str] = Field(default_factory=list)


class QuietHoursConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    start: dt_time
    end: dt_time
    timezone: str = "Europe/Stockholm"

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"Invalid IANA timezone: {v}") from e
        return v

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class TelegramConfig(BaseModel):
    bot_token_env: str
    chat_id_env: str


class TwitterConfig(BaseModel):
    client_id_env: str
    client_secret_env: str
    access_token_path: Path


class MetricsConfig(BaseModel):
    fetch_cron: str
    posts_settle_days: int = Field(ge=1)


class DigestConfig(BaseModel):
    cron: str


class LearningConfig(BaseModel):
    recent_decisions_n: int = Field(ge=0)
    recent_posts_n: int = Field(ge=0)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: Literal["json", "text"] = "json"


class IngestionConfig(BaseModel):
    """Ingestion-side knobs not in the SPEC's primary YAML schema. Defaults
    match the spec's text in §7.1."""

    skip_commit_patterns: list[str] = Field(
        default_factory=lambda: [r"^(chore|ci|docs|style)(\(.+\))?:"]
    )
    first_run_max_age_hours: int = 24


class PersonaConfig(BaseModel):
    """Telegram-only persona controls. Affects how Wire talks to Johan,
    not the X/Twitter post text. Optional — missing = enabled with defaults."""

    enabled: bool = True
    llm_intro_on_drafts: bool = True
    llm_frame_on_digest: bool = True
    model_task: Literal["drafting", "triage", "voice_profile", "digest"] = "triage"


class WireConfig(BaseModel):
    github: GithubConfig
    repos: ReposLocation
    llm: LLMConfig
    session: SessionConfig
    quiet_hours: QuietHoursConfig
    telegram: TelegramConfig
    twitter: TwitterConfig
    metrics: MetricsConfig
    digest: DigestConfig
    learning: LearningConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)


class RepoEntry(BaseModel):
    name: str
    visibility: Literal["public", "private"]
    # Optional override for how the repo is shown to users and to the
    # drafting LLM. Use this to restore intended casing that the GitHub
    # API flattens — e.g. "medianalyzer" → "MediAnalyzer". When unset,
    # `display_name_for()` falls back to capitalizing the first letter.
    display_name: str | None = None
    notes: str = ""

    @field_validator("name")
    @classmethod
    def _no_slash(cls, v: str) -> str:
        if "/" in v:
            raise ValueError(
                f"Repo name {v!r} contains '/'. Use the bare repo name; the org "
                "is taken from github.org in config.yaml."
            )
        return v


class ReposFile(BaseModel):
    repos: list[RepoEntry]

    @field_validator("repos")
    @classmethod
    def _unique_names(cls, v: list[RepoEntry]) -> list[RepoEntry]:
        names = [r.name for r in v]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"Duplicate repo names in repos.yaml: {duplicates}")
        return v

    def names(self) -> set[str]:
        return {r.name for r in self.repos}

    def get(self, name: str) -> RepoEntry | None:
        for r in self.repos:
            if r.name == name:
                return r
        return None


class ConfigError(SystemExit):
    """Raised on any config validation failure — fails the process fast."""


def _load_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping at the top level")
    return raw


def _build_llm_dict_from_env(yaml_llm: dict[str, Any]) -> dict[str, Any]:
    """Assemble the validated `llm` block by reading provider / model / endpoint
    fields from env vars and folding in the operational settings that remain in
    config.yaml (`prompt_caching`, `monthly_budget_usd`, `budget_alert_threshold`).

    Env vars are the source of truth for what model runs where so the live knobs
    can be flipped in Coolify without editing /data/config.yaml. Missing required
    vars surface as ConfigError at startup.
    """
    provider = os.environ.get("WIRE_LLM_PROVIDER", "").strip().lower()
    if not provider:
        raise ConfigError("WIRE_LLM_PROVIDER must be set (claude | llamacpp)")
    if provider not in ("claude", "llamacpp"):
        raise ConfigError(
            f"WIRE_LLM_PROVIDER={provider!r} is invalid; expected 'claude' or 'llamacpp'"
        )

    def _required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} must be set")
        return value

    claude_models = {
        "drafting": _required("WIRE_CLAUDE_DRAFTING_MODEL"),
        "triage": _required("WIRE_CLAUDE_TRIAGE_MODEL"),
        "voice_profile": _required("WIRE_CLAUDE_VOICE_PROFILE_MODEL"),
        "digest": _required("WIRE_CLAUDE_DIGEST_MODEL"),
    }

    llamacpp_block: dict[str, Any] | None = None
    if provider == "llamacpp":
        try:
            timeout = int(os.environ.get("WIRE_LLAMACPP_TIMEOUT_SECONDS", "90"))
        except ValueError as e:
            raise ConfigError(f"WIRE_LLAMACPP_TIMEOUT_SECONDS must be an integer: {e}") from e
        try:
            temperature = float(os.environ.get("WIRE_LLAMACPP_TEMPERATURE", "0.5"))
        except ValueError as e:
            raise ConfigError(f"WIRE_LLAMACPP_TEMPERATURE must be a float: {e}") from e
        llamacpp_block = {
            "base_url": _required("WIRE_LLAMACPP_BASE_URL"),
            "model": _required("WIRE_LLAMACPP_MODEL"),
            "timeout_seconds": timeout,
            "temperature": temperature,
        }

    merged: dict[str, Any] = {
        "provider": provider,
        "claude": claude_models,
        "llamacpp": llamacpp_block,
    }
    # Carry over the operational settings that remain in YAML. Validation
    # (presence / type / range) is left to pydantic so the error messages stay
    # consistent with the rest of the schema.
    for key in ("prompt_caching", "monthly_budget_usd", "budget_alert_threshold"):
        if key in yaml_llm:
            merged[key] = yaml_llm[key]
    return merged


def load_config(path: Path) -> WireConfig:
    raw = _load_yaml_mapping(path)
    yaml_llm = raw.get("llm", {}) or {}
    if not isinstance(yaml_llm, dict):
        raise ConfigError("llm: block in config.yaml must be a mapping")
    raw["llm"] = _build_llm_dict_from_env(yaml_llm)
    try:
        return WireConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid {path}:\n{e}") from e


def load_repos(path: Path) -> ReposFile:
    raw = _load_yaml_mapping(path)
    try:
        return ReposFile.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid {path}:\n{e}") from e
