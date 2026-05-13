"""Tests for `revise_draft`: the NL-edit revision call.

Stubs the LLM provider so the test is deterministic. Verifies the user
instruction lands in the prompt, the response is parsed, and the call is
logged via `log_llm_call` (the convention from CLAUDE.md).
"""

from __future__ import annotations

import json

import pytest

from wire.db import session as db_session
from wire.db.models import Base, LLMCall
from wire.drafting.drafter import revise_draft
from wire.llm.provider import LLMResponse


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "wire.db"
    monkeypatch.setenv("WIRE_DB_PATH", str(db_path))
    db_session.reset_for_tests()
    engine = db_session.init(db_path)
    Base.metadata.create_all(engine)
    yield db_session
    db_session.reset_for_tests()


class _StubProvider:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def complete(self, task, system, messages, response_format=None, max_tokens=600):
        self.calls.append({"task": task, "system": system, "messages": messages})
        return LLMResponse(
            content=json.dumps(self.payload),
            provider="llamacpp",
            model="m",
            input_tokens=20,
            output_tokens=12,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=18,
            cost_usd=0.0,
            task=task,
        )


@pytest.mark.asyncio
async def test_revise_draft_returns_revised_text(db):
    stub = _StubProvider({"text": "rewritten, shorter"})
    out = await revise_draft(
        "an original draft, longer than needed",
        "make it shorter",
        repo_display="Wire",
        provider=stub,
    )
    assert out == "rewritten, shorter"


@pytest.mark.asyncio
async def test_revise_draft_passes_instruction_and_repo_to_prompt(db):
    stub = _StubProvider({"text": "ok"})
    await revise_draft(
        "draft body",
        "drop the emoji",
        repo_display="MediAnalyzer",
        provider=stub,
    )
    user_msg = stub.calls[0]["messages"][0]["content"]
    assert "drop the emoji" in user_msg
    # Repo display name is fed in so the LLM preserves casing.
    assert "MediAnalyzer" in user_msg
    # Original draft is included.
    assert "draft body" in user_msg


@pytest.mark.asyncio
async def test_revise_draft_logs_llm_call(db):
    """log_llm_call must run after every provider.complete (CLAUDE.md
    convention). The row is bucketed under task='drafting' since revisions
    use the same Sonnet-quality drafting tier as the initial draft."""
    stub = _StubProvider({"text": "ok"})
    await revise_draft("draft", "shorter", repo_display="Wire", provider=stub)

    with db.session_scope() as sa:
        rows = sa.query(LLMCall).all()
        assert len(rows) == 1
        assert rows[0].task == "drafting"


@pytest.mark.asyncio
async def test_revise_draft_strips_whitespace(db):
    stub = _StubProvider({"text": "   trimmed   "})
    out = await revise_draft("d", "i", repo_display="W", provider=stub)
    assert out == "trimmed"
