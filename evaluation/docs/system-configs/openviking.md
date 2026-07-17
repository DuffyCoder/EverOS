# OpenViking session-bundle presets

This page records the stable operational constraints shared by the
OpenViking session-bundle family. Use public system IDs from
[`index.yaml`](../../config/systems/index.yaml); the categorized YAML path is
not a command-line interface.

## Ingest and retrieval channels

The ingest matrix is:

- memcore-only: agent-run ingest plus memory-core indexing; no OpenViking SDK;
- OpenViking+memcore: direct OpenViking SDK ingest plus the memcore agent-run
  ingest and index; and
- OpenViking+noop: direct OpenViking SDK ingest only.

The distinction between direct SDK ingest and plugin hooks is important. The
host adapter creates one OpenViking session ID per conversation. For each
LoCoMo sub-session it adds messages to that shared session, commits, and waits
for the task to complete before proceeding. The plugin's `afterTurn` path is
threshold-triggered and commits asynchronously, so it cannot provide the same
ingest completion barrier. At QA time the in-container plugin remains the
retrieval path through its context-engine assemble and `before_prompt_build`
hooks.

## Tenant identity invariants

Host-side writes and in-container reads must use the same account and user
identity. The noop family keeps conversations isolated by setting both
`user_id_template: "{conv_id}"` and `agent_id_template: "{conv_id}"`, with a
per-session task timeout of 3600 seconds. The adapter forwards the resolved
user identity to the plugin container. Agent routing is deliberately
asymmetric: SDK ingest writes the per-conversation agent namespace, while the
plugin's QA path remains on its default agent namespace.

`OPENVIKING_AGENT_PREFIX` is intentionally not forwarded. Adding a synthesized
QA-side agent namespace would query a different path from either namespace
above and break the locked routing behavior.

The memcore+OpenViking variant retains its historical fixed
`user_id: eval-1`, has no agent template, and uses a 600-second task timeout.
Do not change either identity scheme without validating both sides.

## Timeout boundaries

The noop family has two independent limits:

- `openclaw.agent_timeout_seconds: 240` is the outer agent wall-clock limit.
  It gives iterative archive and tool retrieval enough time to finish instead
  of being cut off by the older 90-second framework limit.
- `openclaw.agent_llm.idle_timeout_seconds: 180` is the streaming idle limit
  between provider chunks. It tolerates longer chunk gaps under concurrent
  provider load while the 240-second outer limit still caps total wall time.

Keep the distinction explicit when diagnosing failures: the idle limit is not
the total QA duration, and increasing it does not remove the outer limit.

## Diagnostic and ablation variants

`openclaw-docker-openviking-session-bundle-noop-serial` sets both search and
answer concurrency to one. Its purpose is serial QA-log attribution: each
QA-log time window can be assigned to one question without overlap. It is much
slower than the canonical preset, is diagnostic-only, and is not suitable for
benchmark scoring. It also waits `post_add_wait_seconds: 600` before QA. The
same leaf adds `cleanup_max_retries: 60` and `cleanup_retry_delay_sec: 2` so
transient `409 path_busy` cleanup responses are retried after interrupted
runs.

`openclaw-docker-openviking-session-bundle-noop-fixpack` forwards four plugin
configuration knobs for controlled ablation runs:

- `OPENVIKING_AUTO_CAPTURE`
- `OPENVIKING_RECALL_SCORE_THRESHOLD`
- `OPENVIKING_RECALL_LIMIT`
- `OPENVIKING_RECALL_MAX_INJECTED_CHARS`

These variables are absent from the canonical noop and serial allowlists; do
not move them into the shared base.

The canonical noop and fixpack leaves set `remove_container_on_stop: true`, so
stopped containers are automatically removed. The serial diagnostic leaf sets
it to `false` so its container can be inspected after a run.

## Image rebuild and pinning

Rebuild the container image whenever the OpenViking plugin or evaluation
bridge changes. The memcore+OpenViking preset is pinned to an immutable GHCR
digest. The noop family currently names a local image tag, so operators must
build the matching source before use; for shared or archival runs, publish it
and use digest pinning to prevent a later tag update from changing results.
Record the image identity in run provenance.
