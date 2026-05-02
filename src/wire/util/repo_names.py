"""Helpers for rendering repo names with the casing the user actually wants.

GitHub returns bare lowercase names (`wire`, `medianalyzer`). We pass those
through to the drafting LLM and to user-facing Telegram messages. The user
wants display-friendly casing instead — `Wire`, `MediAnalyzer`. Per-repo
overrides live in `repos.yaml` as the optional `display_name` field; if
absent, we capitalize the first letter as a sensible default.
"""

from __future__ import annotations

from wire.config import ReposFile


def display_name_for(name: str, repos: ReposFile | None) -> str:
    """Return the user-facing name for `name`.

    Looks up `display_name` on the matching `RepoEntry`; if absent or the
    repo isn't in the allowlist, capitalizes the first letter of `name`.
    Empty string in, empty string out.
    """
    if not name:
        return name
    if repos is not None:
        entry = repos.get(name)
        if entry is not None and entry.display_name:
            return entry.display_name
    return name[:1].upper() + name[1:]
