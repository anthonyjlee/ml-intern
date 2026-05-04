"""Verifier tests for ml_intern_research_failed root cause analysis.

Tests cover:
  1. _classify_research_error() maps exception types to stable categories
  2. _acompletion_with_retry() retries transient errors and gives up on others
  3. build_kpis._session_metrics() tracks per-tool failures in _tool_failures_by_name
  4. build_kpis._aggregate() exports tool_failures_by_name_json + research_failures

Run with:
    cd /Users/anthonylee/repos/ml-intern
    python -m pytest tests/unit/test_research_failures.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ─────────────────────────────────────────────────────────────────

def _load_kpis():
    """Load scripts/build_kpis.py without treating scripts/ as a package."""
    path = Path(__file__).parent.parent.parent / "scripts" / "build_kpis.py"
    spec = importlib.util.spec_from_file_location("build_kpis", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_kpis", mod)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def _ev(event_type, data=None, ts="2026-05-04T10:00:00"):
    return {"timestamp": ts, "event_type": event_type, "data": data or {}}


def _session(events, user_id="u1", start="2026-05-04T09:59:00"):
    return {
        "session_id": "sess-" + user_id,
        "session_start_time": start,
        "session_end_time": "2026-05-04T10:05:00",
        "model_name": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hi"}],
        "events": events,
        "user_id": user_id,
    }


# ── RC1 tests: error classification ─────────────────────────────────────────

def test_classify_transient_api_error_by_message():
    """Exception messages containing rate-limit/gateway keywords → transient_api_error."""
    from agent.tools.research_tool import _classify_research_error

    for msg in [
        "rate limit exceeded",
        "429 too many requests",
        "502 bad gateway",
        "503 service unavailable",
        "529 overloaded",
        "timed out",
        "timeout",
    ]:
        exc = RuntimeError(msg)
        assert _classify_research_error(exc) == "transient_api_error", (
            f"Expected transient_api_error for: {msg!r}"
        )


def test_classify_context_limit_by_message():
    """Context-window keywords → context_limit."""
    from agent.tools.research_tool import _classify_research_error

    for msg in [
        "context window exceeded",
        "maximum context length",
        "too many tokens",
    ]:
        exc = RuntimeError(msg)
        assert _classify_research_error(exc) == "context_limit", (
            f"Expected context_limit for: {msg!r}"
        )


def test_classify_unknown_for_generic_errors():
    """Generic exceptions → unknown."""
    from agent.tools.research_tool import _classify_research_error

    assert _classify_research_error(ValueError("something broke")) == "unknown"
    assert _classify_research_error(KeyError("missing key")) == "unknown"


# ── RC3 tests: retry logic ───────────────────────────────────────────────────

class _TransientError(Exception):
    """Simulates a 429 / rate-limit LLM error."""
    pass


class _PermanentError(Exception):
    """Simulates a non-retryable LLM error."""
    pass


def _make_log_fn():
    logged = []
    async def _log(text):
        logged.append(text)
    return _log, logged


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    """_acompletion_with_retry retries once after a transient error."""
    from agent.tools.research_tool import _acompletion_with_retry

    call_count = 0
    fake_response = MagicMock()

    async def _fake_acompletion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("rate limit exceeded")
        return fake_response

    _log, logged = _make_log_fn()

    # Pass the mock directly via the _acompletion_fn injection seam
    # (avoids fighting litellm's decorator chain with patching).
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await _acompletion_with_retry(
            log_fn=_log,
            backoff_base=0.01,  # tiny backoff for test speed
            _acompletion_fn=_fake_acompletion,
            messages=[],
            model="claude-sonnet-4-6",
        )

    assert result is fake_response, "Should return the successful response"
    assert call_count == 2, "Should have called acompletion twice"
    mock_sleep.assert_awaited_once()  # exactly one backoff sleep
    assert any("retry:1" in msg for msg in logged), (
        f"Expected retry log event, got: {logged}"
    )


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_retries():
    """_acompletion_with_retry raises on the final attempt after exhausting retries."""
    from agent.tools.research_tool import _acompletion_with_retry, _MAX_RETRIES

    call_count = 0

    async def _always_fails(**kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("rate limit exceeded")

    _log, _ = _make_log_fn()

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="rate limit exceeded"):
            await _acompletion_with_retry(
                log_fn=_log,
                backoff_base=0.01,
                _acompletion_fn=_always_fails,
                messages=[],
                model="claude-sonnet-4-6",
            )

    assert call_count == _MAX_RETRIES, (
        f"Expected exactly {_MAX_RETRIES} attempts, got {call_count}"
    )


@pytest.mark.asyncio
async def test_no_retry_for_non_transient_errors():
    """Non-transient errors propagate immediately without retry."""
    from agent.tools.research_tool import _acompletion_with_retry

    call_count = 0

    async def _permanent_error(**kwargs):
        nonlocal call_count
        call_count += 1
        raise ValueError("invalid request body")

    _log, _ = _make_log_fn()

    with pytest.raises(ValueError, match="invalid request body"):
        await _acompletion_with_retry(
            log_fn=_log,
            _acompletion_fn=_permanent_error,
            messages=[],
            model="claude-sonnet-4-6",
        )

    assert call_count == 1, "Non-transient errors should NOT be retried"


# ── RC2 tests: per-tool failure tracking in KPI pipeline ────────────────────

def test_session_metrics_tracks_per_tool_failures():
    """_session_metrics populates _tool_failures_by_name for failed tool_output events."""
    mod = _load_kpis()
    events = [
        _ev("tool_call", {"tool": "research"}, ts="2026-05-04T10:00:05"),
        # Research tool failed:
        _ev("tool_output", {
            "tool": "research",
            "success": False,
            "output": "[error:transient_api_error] Research agent LLM error: ...",
        }),
        _ev("tool_call", {"tool": "bash"}, ts="2026-05-04T10:00:10"),
        # bash succeeded:
        _ev("tool_output", {"tool": "bash", "success": True, "output": "ok"}),
    ]
    m = mod._session_metrics(_session(events))

    assert m["tool_calls_total"] == 2
    assert m["tool_calls_success"] == 1
    assert m["_tool_failures_by_name"].get("research") == 1, (
        f"Expected research failure count=1, got: {m['_tool_failures_by_name']}"
    )
    assert m["_tool_failures_by_name"].get("bash", 0) == 0, (
        "bash succeeded — should not appear in failures"
    )
    assert m["_research_failures"] == 1


def test_session_metrics_no_failures_when_all_succeed():
    """_tool_failures_by_name is empty when all tools succeed."""
    mod = _load_kpis()
    events = [
        _ev("tool_call", {"tool": "research"}, ts="2026-05-04T10:00:05"),
        _ev("tool_output", {"tool": "research", "success": True, "output": "great findings"}),
    ]
    m = mod._session_metrics(_session(events))
    assert m["_tool_failures_by_name"] == {}
    assert m["_research_failures"] == 0


def test_aggregate_exports_tool_failures_by_name_json():
    """_aggregate produces tool_failures_by_name_json and research_failures columns."""
    mod = _load_kpis()

    # Session 1: research failed once
    events1 = [
        _ev("tool_call", {"tool": "research"}, ts="2026-05-04T10:00:05"),
        _ev("tool_output", {"tool": "research", "success": False, "output": "[error:transient_api_error] ..."}),
    ]
    # Session 2: research failed twice, bash failed once
    events2 = [
        _ev("tool_call", {"tool": "research"}, ts="2026-05-04T10:00:06"),
        _ev("tool_output", {"tool": "research", "success": False, "output": "[error:context_limit] ..."}),
        _ev("tool_call", {"tool": "research"}, ts="2026-05-04T10:00:07"),
        _ev("tool_output", {"tool": "research", "success": False, "output": "[error:transient_api_error] ..."}),
        _ev("tool_call", {"tool": "bash"}, ts="2026-05-04T10:00:08"),
        _ev("tool_output", {"tool": "bash", "success": False, "output": "error: command not found"}),
    ]

    s1 = mod._session_metrics(_session(events1, user_id="u1"))
    s2 = mod._session_metrics(_session(events2, user_id="u2"))
    agg = mod._aggregate([s1, s2])

    import json
    failures = json.loads(agg["tool_failures_by_name_json"])
    assert failures.get("research") == 3, (
        f"Expected 3 research failures (1+2), got: {failures}"
    )
    assert failures.get("bash") == 1, (
        f"Expected 1 bash failure, got: {failures}"
    )
    assert agg["research_failures"] == 3, (
        f"Expected research_failures=3, got: {agg['research_failures']}"
    )


def test_aggregate_tool_failures_absent_when_all_succeed():
    """tool_failures_by_name_json is {} (not missing) when no failures occurred."""
    mod = _load_kpis()
    events = [
        _ev("tool_call", {"tool": "research"}, ts="2026-05-04T10:00:05"),
        _ev("tool_output", {"tool": "research", "success": True, "output": "great"}),
    ]
    s = mod._session_metrics(_session(events))
    agg = mod._aggregate([s])

    import json
    failures = json.loads(agg["tool_failures_by_name_json"])
    assert failures == {}, f"Expected empty failures dict, got: {failures}"
    assert agg["research_failures"] == 0


# ── Integration: error prefix format ────────────────────────────────────────

def test_error_prefix_is_parseable():
    """[error:X] prefix in tool output can be extracted with a simple regex."""
    import re
    _ERROR_PREFIX = re.compile(r"^\[error:([a-z_]+)\]")

    samples = [
        "[error:transient_api_error] Research agent LLM error: RateLimitError",
        "[error:context_limit] Research context exhausted and summary call failed.",
        "[error:iteration_limit] Research agent hit iteration limit (60).",
    ]
    for s in samples:
        m = _ERROR_PREFIX.match(s)
        assert m is not None, f"Failed to parse error prefix from: {s!r}"
        assert m.group(1) in ("transient_api_error", "context_limit", "iteration_limit"), (
            f"Unexpected category: {m.group(1)!r}"
        )
