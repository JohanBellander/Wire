"""Tests for the README fetch + cache pipeline."""

from __future__ import annotations

import base64
import time

import httpx
import pytest

from wire.config import RepoEntry, ReposFile
from wire.db import session as db_session
from wire.db.models import Base, BotState
from wire.ingestion.github_client import GitHubClient
from wire.ingestion.readme_fetcher import (
    MAX_README_CHARS,
    _strip_badges_and_images,
    _truncate,
    ensure_readme_cached,
    fetch_and_cache_readme,
    get_cached_readme,
    refresh_all_readmes,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


@pytest.fixture
def gh_client():
    """A GitHubClient whose HTTP layer we'll mock per test."""
    c = GitHubClient(app_id=1, installation_id=1, private_key_pem="fake", org="me")
    # Skip JWT exchange — set the cached installation token directly.
    from wire.ingestion.github_client import _CachedToken

    c._installation_token = _CachedToken(token="t", expires_at=time.time() + 3600)
    return c


# ---------- string cleaning ----------


def test_strip_image_lines():
    text = "# Cool repo\n\n![logo](logo.png)\n\nReal description here."
    out = _strip_badges_and_images(text)
    assert "logo.png" not in out
    assert "Cool repo" in out
    assert "Real description here" in out


def test_strip_linked_badges():
    text = (
        "# Project\n\n"
        "[![CI](https://img.shields.io/x.svg)](https://example.com/ci)\n"
        "[![License](https://img.shields.io/y.svg)](https://example.com/license)\n\n"
        "Body text."
    )
    out = _strip_badges_and_images(text)
    assert "shields.io" not in out
    assert "Body text" in out


def test_strip_html_img_tags():
    text = '# Project\n\n<img src="banner.png" width="600">\n\nBody.'
    out = _strip_badges_and_images(text)
    assert "<img" not in out
    assert "Body" in out


def test_strip_collapses_blank_lines():
    text = "Para 1.\n\n\n\n\nPara 2."
    out = _strip_badges_and_images(text)
    # No more than one blank line between paragraphs
    assert "\n\n\n" not in out
    assert "Para 1" in out and "Para 2" in out


# ---------- truncation ----------


def test_truncate_short_unchanged():
    text = "short readme"
    assert _truncate(text) == "short readme"


def test_truncate_at_limit():
    text = "x" * MAX_README_CHARS
    assert _truncate(text) == text


def test_truncate_breaks_on_paragraph_when_possible():
    # Each paragraph is distinct so we can verify which one we cut at.
    paras = [f"This is paragraph number {i}, " + "x" * 250 for i in range(20)]
    text = "\n\n".join(paras)  # 5000+ chars, ~250-char paras with clear breaks
    out = _truncate(text)
    assert "[…truncated]" in out
    assert len(out) < len(text)
    # The cut should land at a paragraph boundary, so the last visible
    # paragraph should be a complete one (i.e., the text right before
    # "[…truncated]" should end with the x-padding from a paragraph, not
    # mid-content).
    body = out[: out.find("[…truncated]")].rstrip()
    # The body should end with "x" characters (paragraph padding) since each
    # paragraph ends in 250 x's, AND the cut should be on a paragraph
    # boundary (so the full last-fitting paragraph is included).
    assert body.endswith("x")


def test_truncate_falls_back_to_hard_cut_when_no_paragraph_break():
    text = "x" * 4000  # one giant block, no paragraph breaks
    out = _truncate(text)
    assert "[…truncated]" in out
    assert len(out) < len(text)


# ---------- bot_state caching ----------


def test_get_cached_returns_none_when_unset(db):
    assert get_cached_readme("winetrackr") is None


# ---------- fetch_and_cache ----------


@pytest.mark.asyncio
async def test_fetch_and_cache_decodes_and_stores(db, gh_client, respx_mock):
    body = "# winetrackr\n\nA wine cellar tracker."
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    respx_mock.get("https://api.github.com/repos/me/winetrackr/readme").mock(
        return_value=httpx.Response(200, json={"content": encoded, "encoding": "base64"})
    )
    try:
        result = await fetch_and_cache_readme(gh_client, "winetrackr")
    finally:
        await gh_client.aclose()

    assert result is not None
    assert "wine cellar tracker" in result
    assert get_cached_readme("winetrackr") == result


@pytest.mark.asyncio
async def test_fetch_returns_none_on_404(db, gh_client, respx_mock):
    respx_mock.get("https://api.github.com/repos/me/empty/readme").mock(
        return_value=httpx.Response(404, text="not found")
    )
    try:
        result = await fetch_and_cache_readme(gh_client, "empty")
    finally:
        await gh_client.aclose()
    assert result is None
    assert get_cached_readme("empty") is None


@pytest.mark.asyncio
async def test_fetch_handles_network_error_gracefully(db, gh_client, respx_mock):
    respx_mock.get("https://api.github.com/repos/me/flaky/readme").mock(
        side_effect=httpx.ConnectError("transient")
    )
    try:
        result = await fetch_and_cache_readme(gh_client, "flaky")
    finally:
        await gh_client.aclose()
    assert result is None
    assert get_cached_readme("flaky") is None


@pytest.mark.asyncio
async def test_fetch_strips_badges_before_storage(db, gh_client, respx_mock):
    body = (
        "# project\n\n"
        "![logo](logo.png)\n"
        "[![CI](https://img.shields.io/badge.svg)](https://x.com)\n\n"
        "What this thing actually does."
    )
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    respx_mock.get("https://api.github.com/repos/me/project/readme").mock(
        return_value=httpx.Response(200, json={"content": encoded, "encoding": "base64"})
    )
    try:
        result = await fetch_and_cache_readme(gh_client, "project")
    finally:
        await gh_client.aclose()
    assert "shields.io" not in result
    assert "logo.png" not in result
    assert "What this thing actually does" in result


@pytest.mark.asyncio
async def test_ensure_readme_cached_skips_when_cached(db, gh_client, respx_mock):
    """ensure_readme_cached is a no-op if a cache entry already exists."""
    # Pre-seed cache
    with db.session_scope() as s:
        s.add(BotState(key="readme:already-here", value="cached body"))
    # Mock that would fail if invoked
    respx_mock.get("https://api.github.com/repos/me/already-here/readme").mock(
        return_value=httpx.Response(500, text="should not be called")
    )
    try:
        await ensure_readme_cached(gh_client, "already-here")
    finally:
        await gh_client.aclose()
    # Cache untouched
    assert get_cached_readme("already-here") == "cached body"


@pytest.mark.asyncio
async def test_ensure_readme_cached_fetches_when_missing(db, gh_client, respx_mock):
    body = "# brandnew\n\nFresh project."
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    respx_mock.get("https://api.github.com/repos/me/brandnew/readme").mock(
        return_value=httpx.Response(200, json={"content": encoded, "encoding": "base64"})
    )
    try:
        await ensure_readme_cached(gh_client, "brandnew")
    finally:
        await gh_client.aclose()
    cached = get_cached_readme("brandnew")
    assert cached is not None and "Fresh project" in cached


@pytest.mark.asyncio
async def test_refresh_all_readmes_iterates_repos(db, gh_client, respx_mock):
    """refresh_all_readmes hits every repo in the allowlist."""
    repos = ReposFile(
        repos=[
            RepoEntry(name="winetrackr", visibility="public", notes=""),
            RepoEntry(name="helmsman", visibility="private", notes=""),
        ]
    )

    def make_response(name):
        body = f"# {name}\n\nThe {name} project."
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return httpx.Response(200, json={"content": encoded, "encoding": "base64"})

    respx_mock.get("https://api.github.com/repos/me/winetrackr/readme").mock(
        return_value=make_response("winetrackr")
    )
    respx_mock.get("https://api.github.com/repos/me/helmsman/readme").mock(
        return_value=make_response("helmsman")
    )

    try:
        n = await refresh_all_readmes(gh_client, repos)
    finally:
        await gh_client.aclose()
    assert n == 2
    assert "winetrackr" in (get_cached_readme("winetrackr") or "")
    assert "helmsman" in (get_cached_readme("helmsman") or "")
