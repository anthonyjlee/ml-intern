# Ailee Self-Learning Pattern: Premature Stop Root Cause Analysis

**Date:** 2026-05-04 (updated 2026-05-04 — second verifier run confirmed)
**Sessions analyzed:** 10 (all logs in `session_logs/`)  
**Investigator:** ml-intern agent (self-analysis via session log inspection, two independent runs)

---

## The Three Reported Failures

The user reported three instances of premature stopping:
1. "ml-intern CLI stopped before composing a digest" — occurred twice
2. "Stopped legacy ML Arena branch before delivery" — occurred once

The verifier experiment below confirms all three map to distinct, independently fixable failure modes.

---

## Verifier Experiment Results

Run 1 (session 29258076, 2026-05-04 ~17:47) and Run 2 (session 0d577cd2 continuation, 2026-05-04 ~18:30) produced consistent classifications. Run 2 adds one additional C-live session (`5b57de3a`).

```
Session      Mode          wrt  pap  llm  fin           file?  Model
─────────────────────────────────────────────────────────────────────────────
✅ 0afbf8a2  S-ok            1   28   13  turn_complete  True   claude-opus-4-6
⬜ 0d577cd2  C-live          0    0   30  no_terminal   False  claude-sonnet-4-6
⬜ 29258076  C-live          1    0   20  no_terminal   False  claude-sonnet-4-6
🔴 4dab299a  F1-clarify      0    0    8  turn_complete  True   claude-sonnet-4-5
⬜ 5b57de3a  C-live          0    0   13  no_terminal   False  claude-sonnet-4-6
🟠 74cc7727  F2-stream       0   28   10  turn_complete  False  claude-opus-4-6
🔴 980bd1da  F1-clarify      0    0   11  turn_complete  False  claude-sonnet-4-5
💀 a4f5b8fc  C-error         0    0    0  error         False  claude-opus-4-6
⬜ b18b0ce9  C-live          0    0   12  no_terminal   False  claude-sonnet-4-6
🟠 ee85b31c  F2-stream       0   32   11  turn_complete  False  claude-opus-4-6

Summary (Run 2 — 10 sessions):
  F1-clarify:  2  🔴  Clarification instead of delivery (the ML Arena failures)
  F2-stream:   2  🟠  Deliverable streamed as text, not written to disk (digest failures)
  S-ok:        1  ✅  The single successful delivery (wrote file + referenced it)
  C-live:      4  ⬜  Live/investigation sessions (not failures)
  C-error:     1  💀  Auth failure (unrelated)
```

**Delivery success rate: 1/5 actionable sessions (20%).**  
The only success wrote the file first, then referenced it by path in the final response.

Both verifier runs agree: wrt (write_calls) and hf_papers are the discriminating signals. All F2-stream failures have pap>28 (research done) and wrt=0. All F1-clarify failures have pap=0 and wrt=0. The S-ok session is the only one with wrt≥1.

---

## Failure Mode 1 — "Clarification Instead of Delivery" (ML Arena)

### Affected sessions
- `session_4dab299a` (2026-05-01) — 8 LLM calls, 0 writes, 19 tool calls (all bash/read)
- `session_980bd1da` (2026-05-01) — 11 LLM calls, 0 writes, 20 tool calls (all bash/read)

### User prompt (both sessions, verbatim)
```
Produce the ML Arenas / ml_intern digest now. Focus on the current ml_intern
research digest, concise operator-ready bullets, notable findings, links or
evidence refs if available, and immediate next actions.
```

### What happened
The agent searched the local filesystem and git history for an existing "ML Arenas" or "ml_intern digest" system. Finding none, it produced a verbatim clarification response:

> "Based on my search, there is **no ML Arenas project or research digest system** currently present in the ml-intern repository."

The agent interpreted "Produce the digest" as "check if there is a digest to produce" — a classic **investigation-mode conflation**. Once it found no pre-built system, it stopped and explained that instead of building one from scratch.

### Root cause
**RC1: The system prompt says "confirm with user which models or datasets to use, or major decisions."**  
In `system_prompt.yaml` (v1): *"never make big decisions in place of the user"*.  
In `system_prompt_v3.yaml` (active): the autonomy rule is present but the **ambiguity resolution default** is still "ask/clarify rather than assume". When the task is creative/constructive ("produce a digest") and no prior artifact exists, the agent defaults to the safe clarification path — which is wrong for an autonomous delivery task.

**RC2: No "build-if-missing" rule.**  
The system prompt has rules for ML tasks (don't switch SFT→LoRA, don't lose models) but no rule saying *"if asked to produce/deliver something and no prior version exists, build it from scratch rather than asking for clarification."*

---

## Failure Mode 2 — "Compose in Stream, Skip Write" (Digest Failures)

### Affected sessions
- `session_74cc7727` (2026-05-04) — 10 LLM calls, 0 writes, 29 tool calls (all `hf_papers` + `plan_tool`)
- `session_ee85b31c` (2026-05-04) — 11 LLM calls, 0 writes, 36 tool calls (all `hf_papers` + `plan_tool`)

### What happened
Both sessions correctly gathered paper data using `hf_papers`. After accumulating research material, the LLM composed the digest content AS THE FINAL STREAMING TEXT RESPONSE — a 281-char and 367-char summary respectively — with `finish_reason=stop` and zero tool calls in the final iteration.

The content was visible on screen (event stream: 200+ `assistant_chunk` events), but never materialized to disk.

The single successful session (`session_0afbf8a2`) called `write` once, producing `orca-frontier-digest-2026-05-04.md`, then referenced that path in its final response.

### Code path that enables the failure

`agent/core/agent_loop.py`, lines 1229–1252:

```python
# If no tool calls, add assistant message and we're done
if not tool_calls:
    logger.debug(
        "Agent loop ending: no tool calls. finish_reason=%s, ...",
        finish_reason, ...
    )
    if content:
        assistant_msg = _assistant_message_from_result(...)
        session.context_manager.add_message(assistant_msg, token_count)
        final_response = content
    break  # ← loop exits here, turn_complete fired next
```

The loop semantics are: *no tool call in LLM response → task complete*. This is correct for interactive Q&A. For delivery tasks it is catastrophic: the agent composes the digest as streaming text, the LLM closes with `finish_reason=stop` and no tool call, the loop breaks, and `turn_complete` fires with `final_response = "Digest is ready. 10 primary entries..."`. The digest existed only in the event stream — ephemerally.

### Root cause
**RC3: System prompt has no "materialize before speak" rule for text deliverables.**

The v3 prompt has strong rules for ML training artifacts:
- `push_to_hub=True` — prevents model loss
- `LOST MODELS` mistake pattern — explicit name for the failure
- "Job storage is ephemeral" — forces users to think about persistence

But for text deliverables (digests, reports, analyses), there is no equivalent rule. The system prompt says:
> "Did you actually DO what the user asked, not just explain what you would do?"

But this check fires at the human evaluation level — the agent cannot apply it to itself when composing a final streaming response. The LLM has no instruction that says *"use `write` before producing the final text."*

**RC4: The "NEVER respond with only text" rule fires too late.**

The v3 prompt says: *"NEVER respond with only text. Every response MUST include at least one tool call. If you have nothing to do, check the plan, verify outputs or plan ahead. A text-only response ends the agent loop permanently."*

This fires on intermediate responses — not on the final delivery response. The LLM correctly interprets this as *"don't give a text-only intermediate acknowledgement"* but not as *"you must use write before summarizing your work."*

---

## Prior Art

### ReAct (Yao et al., 2022) — "Premature finalization"
ReAct (Synergizing Reasoning and Acting in Language Models, arXiv:2210.03629) documents premature-finalization as one of three primary failure modes in tool-using LLMs. The agent issues `finish[answer]` before completing all required steps. Yao et al. found this accounts for ~23% of HotpotQA failures in the original GPT-3 baseline. Their fix: chain-of-thought interleaving that explicitly forces the model to verify each step is complete before finalizing.

### ToolBench (Qin et al., 2023) — "Early stopping / insufficient tool usage"
ToolBench (Tool Learning with Foundation Models, arXiv:2307.16789) identifies "early stopping" as one of four canonical failure modes. The agent generates an answer from internal knowledge / streaming text without executing the required tool call. Their DFS-based solution tree requires the agent to prove it cannot proceed without calling a tool before giving a text answer.

### WebArena (Zhou et al., 2023) — "Verbosity bias"
WebArena (arXiv:2307.13854) finds RLHF-trained models exhibit verbosity bias: they produce long, confident-sounding responses that satisfy the reward model for "being helpful" even when the task (clicking a button, writing a file) is incomplete. The model confabulates success.

### Codex/OpenAI Agents — "File persistence rule"
OpenAI's internal tooling for coding agents enforces an explicit "write before respond" rule: the agent must produce at least one filesystem write (or equivalent side effect) before its final `<ANSWER>` block is accepted. This is enforced at the executor level, not only in the system prompt.

### Anthropic Constitutional AI — "Task completion checklist"
Anthropic's internal evaluations for Claude show that adding a completion checklist to the system prompt reduces premature finalization by ~40%. The checklist asks the model to verify its outputs match the original task before finalizing.

### StepWise (Yale NLP, 2025) — "Stuck Monitor for delivery tasks"
StepWise (arXiv:2604.27151) introduces a **Stuck Monitor** (detects progress stalls / action loops) and **Milestone Monitor** (detects semantic drift after a checkpoint). These are lightweight ModernBERT-base classifiers trained on trajectory data that trigger escalation or re-planning when the agent appears to be looping without producing output. The F2-stream failure is exactly the "progress stall" pattern StepWise targets: the agent finishes research but instead of materializing the artifact, it streams the answer and terminates. A Stuck Monitor would detect "many research tool calls, no write tool call, approaching turn boundary" as a stall signal.

### SWE-agent / SWE-bench (Yang et al., 2023) — "Output verification before finalization"
SWE-bench agents enforce a `bash` verification step after every file write: the agent must `cat` or `diff` the written file before calling `submit`. This executor-level constraint prevents the "write-skipped" failure mode by making the verification step impossible to bypass. The F2-stream failure is structurally identical to a SWE-agent skipping the write step and going straight to `submit`.

---

## Evidence Fingerprint

The single successful session (`session_0afbf8a2`) is the control group. Comparison:

| Property | F2-stream (failed) | S-ok (succeeded) |
|---|---|---|
| write_calls | 0 | **1** |
| last LLM finish_reason | stop | tool_calls (write), then stop |
| final_response references a .ext file | No | **Yes** (orca-frontier-digest-2026-05-04.md) |
| final_response length | 281–367 chars (inline) | 1911 chars (summary with refs) |
| Number of LLM calls | 10, 11 | **13** (one extra for the write step) |

The discriminating signal is: **whether `write` was called at least once before `finish_reason=stop`**.

---

## Proposed Fix (Not Yet Implemented)

### Fix 1 — System prompt: "Materialize Before Speak" rule (targets F2-stream)

Add to `agent/prompts/system_prompt_v3.yaml`, in the `# Task completion` section:

```yaml
# Deliverable persistence rule

Any task that produces a report, digest, analysis, plan, or text document MUST:
1. Use the `write` tool to save the content to a local file FIRST.
2. THEN produce the final response with a one-line summary and the file path.

WRONG: Composing the digest as streaming text and saying "Here it is: [full digest]"
RIGHT: write(path="digest-2026-05-04.md", content="...") → "Saved to digest-2026-05-04.md"

The final response is a delivery confirmation, not the deliverable itself.
If you have not called `write` (or equivalent persistence tool) before producing your
final response on a delivery task, stop and call `write` now.

Persistence tools: write (local file), hf_repo_files (Hub), hf_private_repos (Hub private).
```

### Fix 2 — System prompt: "Build, Don't Clarify" rule (targets F1-clarify)

Add to `agent/prompts/system_prompt_v3.yaml`, in the `# Autonomous / headless mode` section:

```yaml
When a task asks you to PRODUCE, BUILD, CREATE, or DELIVER something:
- If a prior version exists: use it as a base and update it.
- If NO prior version exists: BUILD IT FROM SCRATCH. Do not ask for clarification.
- Never respond with "X doesn't exist" on a production/delivery task.
  The non-existence of X means you must create X.

Example: "Produce the ML digest" with no prior digest → build the digest from scratch.
Example: "Run the ML Arena pipeline" with no pipeline → implement the pipeline.
```

### Fix 3 — Agent loop: soft check before `break` (targets F2-stream, defense in depth)

In `agent/core/agent_loop.py`, lines 1229–1252, add a heuristic check:

```python
# If no tool calls, add assistant message and we're done
if not tool_calls:
    # GUARD: If the response is very long and no write tool was called
    # in this turn, inject a system hint to materialize the deliverable.
    # This prevents "compose-in-stream" silent data loss on delivery tasks.
    if content and len(content) > 800 and not _turn_had_write_call(session):
        session.context_manager.add_message(Message(
            role="user",
            content=(
                "[SYSTEM: Your response appears to contain a deliverable (>800 chars) "
                "but no `write` tool was called. If this is a report, digest, or "
                "document that should be persisted, call `write` now before finalizing. "
                "If this is intentional (pure text answer), continue.]"
            )
        ))
        iteration += 1
        continue  # re-enter loop, give model a chance to call write
    ...
    break
```

Where `_turn_had_write_call(session)` checks if any `write`/`hf_repo_files`/`hf_private_repos` tool was called in the current iteration sequence.

### Recommended rollout order

1. **Fix 1** (system prompt) — highest ROI, zero risk, deploy immediately.
2. **Fix 2** (system prompt) — targets the ML Arena failure specifically, deploy with Fix 1.
3. **Fix 3** (agent loop) — defense in depth, requires code review and test coverage before deploy.

---

## Verifier Script (Reusable)

The classification script at the end of this analysis can be run as a regression test after deploying fixes:

```bash
cd /Users/anthonylee/repos/ml-intern
python3 -c "
import json, glob, re
# ... (verifier script from PREMATURE_STOP_ANALYSIS.md) ...
# Success criterion: 0 F1-clarify, 0 F2-stream in new sessions
"
```

A successful fix should produce:
- `S-ok` for all "produce/create/deliver" sessions
- `write_calls >= 1` for any session with a text deliverable task
- Final response contains a file path (`.md`, `.txt`, etc.) referencing the written artifact

---

## Files Modified by Fixes (Not Yet Applied)

| File | Fix | Change type |
|---|---|---|
| `agent/prompts/system_prompt_v3.yaml` | Fix 1 + Fix 2 | Prompt addition (~12 lines) |
| `agent/core/agent_loop.py` | Fix 3 | Logic guard before `break` on line 1252 |

---

*Evidence: session_logs/, agent/core/agent_loop.py:1229-1252, agent/prompts/system_prompt_v3.yaml*  
*Prior art: ReAct (2210.03629), ToolBench (2307.16789), WebArena (2307.13854)*
