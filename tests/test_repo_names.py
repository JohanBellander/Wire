"""Repo display-name resolution.

GitHub returns lowercase repo names; the user wants capitalized forms shown
to humans and to the drafting LLM. Per-repo `display_name` overrides in
repos.yaml win when set; otherwise we capitalize the first letter.
"""

from __future__ import annotations

from wire.config import RepoEntry, ReposFile
from wire.util.repo_names import display_name_for


def _repos(*entries: RepoEntry) -> ReposFile:
    return ReposFile(repos=list(entries))


def test_explicit_display_name_wins():
    repos = _repos(RepoEntry(name="medianalyzer", visibility="public", display_name="MediAnalyzer"))
    assert display_name_for("medianalyzer", repos) == "MediAnalyzer"


def test_falls_back_to_first_letter_capitalize():
    repos = _repos(RepoEntry(name="winetrackr", visibility="public"))
    assert display_name_for("winetrackr", repos) == "Winetrackr"


def test_unknown_repo_still_capitalizes():
    """A repo not in the allowlist (shouldn't happen post-filter, but defensive)
    still renders with first-letter capitalization rather than blowing up."""
    repos = _repos(RepoEntry(name="known", visibility="public"))
    assert display_name_for("ghost-repo", repos) == "Ghost-repo"


def test_none_repos_falls_back_to_capitalize():
    """When the caller doesn't have a ReposFile (e.g. early boot), the helper
    still returns a sane string."""
    assert display_name_for("wire", None) == "Wire"


def test_empty_string_passthrough():
    assert display_name_for("", None) == ""


def test_already_capitalized_unchanged():
    repos = _repos(RepoEntry(name="Wire", visibility="public"))
    assert display_name_for("Wire", repos) == "Wire"
