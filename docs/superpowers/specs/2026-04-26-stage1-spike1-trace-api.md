# Stage 1 Spike #1 — Openclaw Trace API Capability

> **Date**: 2026-04-26
> **Goal**: Determine whether tool-call sequences and per-tool latency can be observed for Stage 2 capability dimension scoring.

---

## TL;DR

**Trace IS feasible via `--verbose on` stderr parsing.** No need for hooks or
log-level=debug or any out-of-band channel. Stage 2 capability metrics
(tool-call frequency, per-tool latency, memory tool usage rate) are all
implementable with a small stderr line parser.

**Cost**: ~2.8 KB extra stderr per agent run (11 KB vs 8 KB at baseline).
**Latency overhead**: not measured, looks negligible.

---

## What We Get from `--verbose on`

Stderr now includes structured `[agent]` events:

```
[agent] embedded run start: runId=trace_v1 sessionId=trace_v1 provider=sophnet model=gpt-4.1-mini thinking=off messageChannel=unknown
[agent] embedded run prompt start: runId=trace_v1 sessionId=trace_v1
[agent] [context-diag] pre-prompt: sessionKey=agent:main:explicit:trace_v1 messages=0 roleCounts=none historyTextChars=0 maxMessageTextChars=0 historyImageBlocks=0 systemPromptChars=21990 promptChars=83 promptImages=0 provider=sophnet/gpt-4.1-mini sessionFile=/path/to/session.jsonl
[agent] embedded run agent start: runId=trace_v1
[agent] embedded run tool start: runId=trace_v1 tool=memory_search toolCallId=call_BFEEImaAROTWZTNzy9CGw5SY
[agent] embedded run tool end:   runId=trace_v1 tool=memory_search toolCallId=call_BFEEImaAROTWZTNzy9CGw5SY
[agent] embedded run agent end: runId=trace_v1 isError=false
[agent] embedded run prompt end: runId=trace_v1 sessionId=trace_v1 durationMs=6886
[agent] embedded run done: runId=trace_v1 sessionId=trace_v1 durationMs=7651 aborted=false
```

Plus `[diagnostic]` events for queue / lane state, and `[memory]` events for
sync attempts. All emit at INFO level when verbose is on.

## What's Captureable (per-question, per-conv, per-run)

| Metric | Source | Reliability |
|---|---|---|
| Tool name + toolCallId | `embedded run tool start:` line | ✅ exact |
| Per-tool start/end timestamps | `tool start:` / `tool end:` paired by toolCallId | ✅ exact |
| Per-tool duration | end - start (line emit time) | ⚠️ stderr line emit ≈ tool boundary, ms-ish |
| Tool call count per QA | count `tool start:` lines per `runId` | ✅ exact |
| Memory tool usage rate | filter to `tool=memory_search` or `tool=memory_get` | ✅ exact |
| Tool sequence | order of `tool start:` lines | ✅ exact |
| System prompt chars | `[context-diag] pre-prompt: systemPromptChars=N` | ✅ exact (already in non-verbose meta) |
| Run total duration | `embedded run done: durationMs=N` | ✅ exact (already in non-verbose meta) |
| Aborted flag | `embedded run done: aborted=...` | ✅ exact (already in non-verbose meta) |
| Was error | `embedded run agent end: isError=...` | ✅ exact |

## What's NOT Captureable (acceptable for Stage 2 Track C)

- **Tool input arguments**: not in stderr; only toolCallId + tool name. Can be
  inferred from the agent's response or stored separately, but not from this
  trace channel.
- **Tool result content**: not emitted. We see the call boundary but not what
  came back from `memory_search`.
- **LLM reasoning between tool calls**: stderr emits at tool / run boundary,
  not at LLM token boundary.
- **Embedded LLM call latency**: only the wrapping run's durationMs is emitted.
  Per-LLM-call timing not observable.

For Stage 2 capability dimensions (AR / TTL / LRU / CR), the **invocation count**
and **timing pattern** are the load-bearing signals — content of tool results
isn't needed.

---

## Implementation: Bridge Side

`evaluation/scripts/openclaw_eval_bridge.mjs::handleAgentRun` already strips
ANSI and parses JSON from stderr. Adding tool-call extraction is a focused
extension:

```javascript
// New: tool-call line patterns (only matched when verbose=on)
const TOOL_START_RE = /^\[agent\] embedded run tool start:\s+runId=(\S+)\s+tool=(\S+)\s+toolCallId=(\S+)/;
const TOOL_END_RE   = /^\[agent\] embedded run tool end:\s+runId=(\S+)\s+tool=(\S+)\s+toolCallId=(\S+)/;
const RUN_DONE_RE   = /^\[agent\] embedded run done:.*durationMs=(\d+).*aborted=(\S+)/;

function extractToolInvocations(stderrText, runId) {
  // Returns [{tool, toolCallId, sequence, start_byte, end_byte}]
  // Pair start/end by toolCallId; preserve sequence by start order.
  // ...
}
```

The harness adds `verbose: true` to the `agent_run` BridgeCommand payload;
bridge appends `--verbose on` to the openclaw CLI args; on success, response
includes `tool_invocations: [{tool, toolCallId, sequence}]` in addition to
the existing `tool_names` (which we already extract from systemPromptReport).

**Note**: existing `meta.systemPromptReport.tools.entries[].name` lists
**registered** tools (the agent's available toolset). The new
`tool_invocations` is the **actually called** subset. These are different
metrics; both have value:
- registered: "did plugin swap work? did memory tools get registered?"
- called: "did agent decide to use memory? how many times?"

---

## Implementation: Adapter Side

`OpenClawAdapter._generate_answer_via_agent` adds an opt-in flag from yaml:

```yaml
# openclaw-agent-local.yaml
openclaw:
  trace:
    verbose: true              # opt-in for Stage 2 capability metrics
```

```python
async def _generate_answer_via_agent(self, query, conv_id, qid):
    sandbox = self._sandbox_for(conv_id)
    payload = {
        **self._bridge_base_payload(sandbox),
        "command": "agent_run",
        "session_id": f"{conv_id}__{qid}",
        "message": query,
        "timeout_seconds": int(self._openclaw_cfg.get("agent_timeout_seconds", 180)),
        "verbose": bool(self._openclaw_cfg.get("trace", {}).get("verbose", False)),
    }
    resp = await arun_bridge(...)
    # ...
    self._append_events(sandbox, [{
        "event": "agent_run_complete",
        "conversation_id": conv_id, "question_id": qid,
        "duration_ms": resp.get("duration_ms"),
        "tool_invocations": resp.get("tool_invocations", []),  # ⭐ Stage 2
        # ... existing fields ...
    }])
```

---

## Stage 2 Capability Metrics Now Implementable

With `tool_invocations` in events.jsonl, scorecard report can derive:

```
Per-plugin × per-condition aggregates:
  - mean tool_calls_per_qa             # heaviness of agent loop
  - p50/p95 tool_calls_per_qa          # tail behavior
  - memory_tool_invocation_rate        # fraction of QAs that called memory_*
  - mean memory_calls_per_qa           # avg memory tool invocations
  - tool_diversity                     # unique tool names called
  - tool_sequence_pattern              # most common tool sequences
```

For AMB-style capability dimensions:
- **AR (Accurate Retrieval)**: did memory_search get called for memory questions?
- **TTL (Test-Time Learning)**: did memory_search find newly-added facts?
- **LRU (Least Recently Used)**: usage pattern over conversation length
- **CR (Conflict Resolution)**: tool call frequency on contradictory QAs

Implementation is per-condition postprocessing of events.jsonl; no further
trace plumbing needed.

---

## Cost / Performance

- Verbose ON: stderr 11,222 bytes (2.85 KB extra vs 8,372 baseline)
- Per agent run: ~7-8s for simple QA, no perceived slowdown from verbose

For 50 QA × N=3 = 150 runs, extra stderr volume is ~430 KB total. Negligible.

---

## Risks

### R-S1-1: Stderr line-emit timing ≠ tool execution boundary

The `tool start:` line is emitted right before tool dispatch; `tool end:` right
after. There's some Node.js logging buffering between actual tool call and stderr
line emit. For 100ms+ tools (LLM-backed memory_search), the difference is <1%.
For sub-10ms tools, % error grows but absolute error is small.

**Mitigation**: accept ms-level timing from this channel; if Stage 2 wants
microsecond precision, use openclaw's hooks system (out of scope for spike).

### R-S1-2: Verbose flag changes agent behavior?

Theoretically, no. `--verbose on` only affects logging level. Should not
change LLM prompt, tool registration, or agent loop semantics. But **must
verify**: re-run accuracy comparison with verbose ON vs OFF on same conv/QA
during Stage 1 Week 4 robustness tests.

### R-S1-3: Stderr line format may evolve

Format strings like `embedded run tool start: runId=... tool=... toolCallId=...`
are not part of openclaw's public API. A regex parser may break on
openclaw upgrades.

**Mitigation**: pin openclaw commit (already done in v0.7); add unit test
that parses captured stderr from D1 smoke; update parser when openclaw
upgrade requires.

---

## Spike #1 Decision

**Stage 2 Track C (trace-level metrics) is GO**. Implementation deferred
to Stage 2 since Stage 1 Week 1-3 doesn't need it for plugin baseline
comparison. But:

- Add yaml `openclaw.trace.verbose` field placeholder to v0.7 design (already
  there; just needs to be wired up)
- Plan Stage 2 Week 1 task: implement `extractToolInvocations` in bridge,
  add unit tests with synthetic verbose stderr fixtures
- Plan Stage 2 Week 2 task: scorecard aggregation over `tool_invocations`

---

## Stage 1 R&D Spike Status

```
Spike #1 (trace API):              ✅ DONE — verbose stderr parsing feasible
Spike #2 (plugin effort):          ✅ DONE — 3.5-4 days mem0, 1.5-2 days subsequent
Spike #3 (mem0 SDK contract):      ⏳ Week 3 Day 1 (when starting mem0 plugin)
```

Both blocking spikes complete. Stage 1 Week 1 Day 1 unblocked — proceed
to Dockerfile.eval + entrypoint.sh.
