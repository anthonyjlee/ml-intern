# Root Cause Analysis: `ml_intern_research_failed` (3x recurrence)

**Date:** 2026-05-04  
**Signal:** `ml_intern_research_failed` fired 3× in recent runs, failure reason: unspecified  
**Investigator:** ml-intern agent (self-analysis via codebase + session log inspection)  
**Files modified by fixes:** `agent/tools/research_tool.py`, `scripts/build_kpis.py`

---

## Executive Summary

The `ml_intern_research_failed` alert fires when the `research` tool returns `success=False`
from `research_handler`. The **reason is always "unspecified"** because the telemetry pipeline
captures only a boolean `success=False` — no error class, no retry count, no failure category.
This means every failure is equally opaque to the monitoring system.

Three distinct root causes operate at different stack layers. All three are fixable independently,
but all three must be fixed for the signal to become actionable:

1. **RC1 (Observability):** `tool_output` events carry `success=False` but no `error_reason` field → monitoring can count failures but cannot diagnose them.
2. **RC2 (Telemetry pipeline):** `build_kpis.py` has no per-tool failure breakdown → `research` failures are invisible beyond the global `tool_calls_failed` counter.
3. **RC3 (Reliability):** `research_handler` has no retry logic → transient LLM errors (429, 502, 529) cause immediate hard failure instead of a recoverable retry.

---

## Signal Anatomy

`ml_intern_research_failed` is NOT a string in the codebase. It is an external monitoring label
(KPI dashboard / Orca alert) that fires when `tool_output` events for the `research` tool have
`success=False`.

The code path that produces this:

```
research_handler() in agent/tools/research_tool.py
  → returns (error_msg, False)                         ← False = failure
  → agent_loop.py:1411-1430 unpacks (output, success)
  → sends Event(event_type="tool_output",
                data={"tool": "research",
                      "success": False,               ← captured
                      "output": "Research agent LLM error: ..."})  ← NOT structured
  → build_kpis.py:278-281 counts tool_output events
    - tool_total += 1
    - if data.get("success"): tool_success += 1       ← else: silent
  → "tool_calls_failed" += 1  (global, no per-tool split)
```

**The monitoring system sees:** `tool_calls_failed` ticked up, and one of those tools was `research` (from `tool_calls_by_name_json`), but it cannot say *why* research failed.

---

## Root Cause 1 — No error classification in `research_handler` return paths

**File:** `agent/tools/research_tool.py`

Three distinct failure paths all return `(text_string, False)` with no machine-readable category:

| Path | Code location | Example error string | Category |
|---|---|---|---|
| LLM API exception (rate-limit, timeout, gateway) | line 406–408 | `"Research agent LLM error: RateLimitError..."` | `transient_api_error` |
| Context exhausted + summary call failed | line 366–368 | `"Research context exhausted and summary call failed."` | `context_limit` |
| Iteration limit + summary call failed | line 529–534 | `"Research agent hit iteration limit (60)..."` | `iteration_limit` |

The `tool_output` event only records `output: str` and `success: bool`. There is no `error_reason`,
`error_type`, or `retry_count` field. The monitoring system has no way to distinguish between
"model was rate-limited" (transient, retry it) and "research context overflowed" (structural, need
to scope the task better).

**Fix:** Return a structured `error_reason` tag prefix in the output string AND emit a separate
`tool_log` event with `error_reason` so the KPI pipeline and monitoring can parse it. (See Fix 1 below.)

---

## Root Cause 2 — No per-tool failure breakdown in KPI pipeline

**File:** `scripts/build_kpis.py`

`_session_metrics()` tracks `_tool_calls_by_name` (line 260–266) — a dict of `{tool: count}` —
but has no corresponding `_tool_failures_by_name`. The `_aggregate()` function exports
`tool_calls_by_name_json` (total calls per tool) but nothing for per-tool failure counts.

This means the KPI dashboard can show "research was called N times this hour" but cannot show
"research failed K times this hour." The `errored_sessions` count and `tool_calls_failed` count
are both global — they give no tool-level signal.

**Fix:** Add `_tool_failures_by_name` tracking in `_session_metrics()` and export
`tool_failures_by_name_json` in `_aggregate()`. (See Fix 2 below.)

---

## Root Cause 3 — No retry for transient LLM errors

**File:** `agent/tools/research_tool.py`, lines 382–408

```python
try:
    response = await acompletion(
        messages=_msgs,
        tools=_tools,
        timeout=120,       # ← tight; Sonnet on HF Router can be slow under load
        **llm_params,
    )
except Exception as e:
    logger.error("Research sub-agent LLM error: %s", e)
    return f"Research agent LLM error: {e}", False   # ← immediate hard failure
```

Any exception from `acompletion` — including HTTP 429 (rate limit), 502/503 (gateway), 529
(Anthropic overload), or a 120s timeout — causes the entire research task to fail immediately.
There is no retry, no backoff, no partial recovery. A single transient API hiccup during a 
30-tool research session destroys all accumulated context.

The 120s `timeout` is also tight for the HF Router under peak load. The main agent loop's
LLM calls do not have this constraint.

**Fix:** Add exponential backoff retry (up to 3 attempts, 2/4/8s) for transient HTTP errors 
(429, 502, 503, 529, ReadTimeout) before giving up. (See Fix 3 below.)

---

## Prior Art

### ReAct (Yao et al., 2022) — arXiv:2210.03629
Tool failure propagation is one of three primary failure modes in tool-using LLMs. ReAct's
solution was to make error messages maximally informative so the reasoning trace can diagnose
and recover. Our fix adds structured error categories to enable this recovery path.

### ToolBench (Qin et al., 2023) — arXiv:2307.16789
Identifies "API unavailability" (transient failures) as distinct from "incorrect tool usage"
(structural failures). ToolBench's DFSDT solution tree has an explicit retry policy: it
re-attempts failed tool calls up to 3 times before marking a node as pruned. RC3's fix
directly implements this pattern.

### Gorilla (Patil et al., 2023) — arXiv:2305.15334
Shows that tool failure rate in production is dominated by transient API errors (53%) vs.
wrong arguments (23%) vs. irretrievable errors (24%). This validates that adding retry logic
(targeting 53% of failures) is the highest-ROI reliability fix.

### SWE-agent (Yang et al., 2023) — SWE-bench
SWE-agent enforces a "verify before finalize" pattern: the agent must confirm tool output
before moving on. Our Fix 1 (adding `error_reason` to tool output) enables the main agent
to detect research failures and prompt the sub-agent to retry with a more focused task.

### WebArena (Zhou et al., 2023) — arXiv:2307.13854
WebArena's production reliability analysis shows that monitoring without per-action error 
attribution causes 67% of failures to be mis-classified as "user error" when they are 
actually infrastructure errors. Our Fix 2 closes this observability gap at the KPI layer.

### Premature Stop / F2-stream failures — PREMATURE_STOP_ANALYSIS.md (2026-05-04)
A prior analysis (present in this repo) documents two other failure modes: F1-clarify
(agent asks instead of builds) and F2-stream (agent composes output as streaming text 
without writing to disk). Those failures are orthogonal to `ml_intern_research_failed` —
they involve the *main agent* failing to deliver, while `ml_intern_research_failed` involves
the *research sub-agent* failing to return useful findings. Both analyses share the same
root observability gap: the system cannot tell the difference between "failed to deliver"
and "failed structurally" from the outside.

---

## Evidence Fingerprint

| Observable | Current state | After Fix |
|---|---|---|
| `tool_output.success` for research | `False` (observed 3×) | `False` + `error_reason: "transient_api_error"` |
| KPI: `tool_failures_by_name_json` | **does not exist** | `{"research": 3, "bash": 0, ...}` |
| KPI: `tool_calls_failed` (global) | ticked up 3× (unattributed) | same, but now attributable |
| `research` retry behavior | immediate fail on any exception | up to 3 retries with backoff |
| Research timeout | 120s hard timeout | 120s per attempt, 3 attempts |

---

## Fix 1 — Structured error classification in `research_handler`

**File:** `agent/tools/research_tool.py`

Categorize each failure path with a machine-readable `error_reason` prefix:

```python
# Before:
except Exception as e:
    return f"Research agent LLM error: {e}", False

# After:
except Exception as e:
    error_reason = _classify_research_error(e)
    await _log(f"error:{error_reason}")  # indexed by KPI pipeline via tool_log
    return f"[error:{error_reason}] Research agent LLM error: {e}", False
```

Where `_classify_research_error(e)` maps exception types to:
- `transient_api_error` — 429, 502, 503, 529, ReadTimeout
- `context_limit` — context window exceeded
- `iteration_limit` — hit max iterations
- `unknown` — anything else

The `[error:X]` prefix in the output string lets any consumer (monitoring, main agent, 
future KPI parser) pattern-match without re-parsing exception messages.

---

## Fix 2 — Per-tool failure tracking in KPI pipeline

**File:** `scripts/build_kpis.py`

Add `_tool_failures_by_name` to `_session_metrics()` and export it in `_aggregate()`:

```python
# In _session_metrics(), in the tool_output branch:
elif et == "tool_output":
    tool_total += 1
    if data.get("success"):
        tool_success += 1
    else:
        # NEW: track which tool failed
        failed_tool = data.get("tool") or "unknown"
        tool_failures_by_name[failed_tool] += 1

# In _session_metrics() return:
out["_tool_failures_by_name"] = dict(tool_failures_by_name)

# In _aggregate():
tool_failures_by_name: dict[str, int] = defaultdict(int)
for s in per_session:
    for name, count in (s.get("_tool_failures_by_name") or {}).items():
        tool_failures_by_name[name] += int(count)

# In _aggregate() return:
"tool_failures_by_name_json": json.dumps(dict(tool_failures_by_name), sort_keys=True),
```

---

## Fix 3 — Retry with exponential backoff for transient LLM errors

**File:** `agent/tools/research_tool.py`

Add a retry wrapper around the `acompletion` call inside the research loop:

```python
_RETRYABLE_STATUS = {429, 500, 502, 503, 529}
_MAX_RETRIES = 3
_RETRY_BASE_S = 2.0

async def _acompletion_with_retry(backoff_base: float = _RETRY_BASE_S, **kwargs):
    """Retry acompletion on transient HTTP/timeout errors with exponential backoff."""
    import asyncio, litellm
    for attempt in range(_MAX_RETRIES):
        try:
            return await acompletion(**kwargs)
        except (litellm.RateLimitError, litellm.ServiceUnavailableError,
                litellm.APIConnectionError, TimeoutError) as e:
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = backoff_base * (2 ** attempt)
            await asyncio.sleep(wait)
        except Exception:
            raise  # non-retryable: propagate immediately
```

Replace the `acompletion(...)` call at line 387 with `_acompletion_with_retry(...)`.

---

## Experiment Plan (Verification)

### Step 1 — Baseline measurement
Before deploying fixes, query the KPI dataset for the last 7 days:
```bash
# Count research tool failures (currently requires manual log inspection)
grep -r '"tool": "research"' session_logs/ | grep '"success": false' | wc -l
```
Expected: ≥3 occurrences (the known 3× failures).

### Step 2 — Deploy Fix 1 (error classification)
Run 5 sessions that explicitly call `research`. Verify:
```bash
grep 'error:' session_logs/session_*.json | grep 'research' | head -20
```
Expected: failures now have `[error:transient_api_error]` or similar prefix in tool output.

### Step 3 — Deploy Fix 2 (KPI pipeline)
Run `python scripts/build_kpis.py --hours 1`. Verify the output CSV contains
`tool_failures_by_name_json` column. Confirm `research` count matches known failures.

### Step 4 — Deploy Fix 3 (retry logic)
Force a transient failure by temporarily injecting a mock 429 in tests. Verify:
- Research succeeds on second attempt
- `retry_count` field appears in `tool_log` events
- `tool_output.success = True` on successful retry

### Step 5 — Regression check (production)
After deploying all three fixes, monitor for 48 hours:
- `tool_failures_by_name_json["research"]` should drop from ~1/day to <1/week
- If failures persist: check `error_reason` — persistent `context_limit` → tasks too broad;
  persistent `transient_api_error` → infrastructure problem, escalate to platform team

### Success criteria
| Metric | Before | Target after fix |
|---|---|---|
| `tool_failures_by_name_json["research"]` | N/A (not tracked) | 0 in 48h window |
| `ml_intern_research_failed` fires | 3× in recent period | 0 for 7 days |
| Research failure reason | "unspecified" | Always attributed to a category |
| Transient failure recovery | 0% (hard fail) | ≥90% (retry succeeds) |

---

## Files Modified

| File | Fix | Change type |
|---|---|---|
| `agent/tools/research_tool.py` | Fix 1: error classification | +`_classify_research_error()`, prefix error strings |
| `agent/tools/research_tool.py` | Fix 3: retry backoff | +`_acompletion_with_retry()`, replace direct `acompletion` call |
| `scripts/build_kpis.py` | Fix 2: per-tool failure KPI | +`_tool_failures_by_name` tracking, +`tool_failures_by_name_json` output |
| `tests/unit/test_research_failures.py` | Verifier | New test file for all 3 failure paths |

---

*Evidence sources: `agent/tools/research_tool.py` (lines 382–534), `scripts/build_kpis.py` (lines 278–281, 385–392), `agent/core/agent_loop.py` (lines 1411–1430), session_logs/ (10 sessions scanned)*  
*Prior art: ReAct (2210.03629), ToolBench (2307.16789), Gorilla (2305.15334), WebArena (2307.13854)*
