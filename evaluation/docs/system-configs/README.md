# Evaluation system configurations

[`evaluation/config/systems/index.yaml`](../../config/systems/index.yaml) is
the public registry. Select a system with its registry ID (for example,
`openclaw-docker-openviking-session-bundle-noop`), never with a YAML filename.
The registry path is an internal implementation detail and may move without
changing the public ID.

## Categories

- `canonical/` contains active, supported benchmark presets.
- `experiments/` contains exploratory variants whose behavior may still move.
- `ablations/` contains controlled one-variable comparisons.
- `tooling/` contains diagnostic presets that are not benchmark defaults.
- `alias` entries exist only in `index.yaml` and preserve compatibility IDs.
- `_bases/` contains inheritance fragments and is not a public category.

## Catalog

The registry exposes exactly these 36 public IDs. Alias rows intentionally have
no physical configuration; their canonical target owns the runnable YAML.

| ID | Category | Status | Adapter | Canonical target | Physical config | Intended use |
|---|---|---|---|---|---|---|
| `evermemos` | `canonical` | `active` | `evermemos` | `evermemos` | [`canonical/evermemos.yaml`](../../config/systems/canonical/evermemos.yaml) | In-process EverMemOS benchmark preset. |
| `evermemos_cloud_api` | `canonical` | `active` | `evermemos_api` | `evermemos_cloud_api` | [`canonical/evermemos_cloud_api.yaml`](../../config/systems/canonical/evermemos_cloud_api.yaml) | EverMemOS cloud API benchmark preset. |
| `evermemos_local_api` | `canonical` | `active` | `evermemos_api` | `evermemos_local_api` | [`canonical/evermemos_local_api.yaml`](../../config/systems/canonical/evermemos_local_api.yaml) | EverMemOS local API benchmark preset. |
| `mem0` | `canonical` | `active` | `mem0` | `mem0` | [`canonical/mem0.yaml`](../../config/systems/canonical/mem0.yaml) | Mem0 API benchmark preset. |
| `memos` | `canonical` | `active` | `memos` | `memos` | [`canonical/memos.yaml`](../../config/systems/canonical/memos.yaml) | MemOS API benchmark preset. |
| `memu` | `canonical` | `active` | `memu` | `memu` | [`canonical/memu.yaml`](../../config/systems/canonical/memu.yaml) | MemU API benchmark preset. |
| `zep` | `canonical` | `active` | `zep` | `zep` | [`canonical/zep.yaml`](../../config/systems/canonical/zep.yaml) | Zep API benchmark preset. |
| `hermes-holographic` | `canonical` | `active` | `hermes` | `hermes-holographic` | [`canonical/hermes-holographic.yaml`](../../config/systems/canonical/hermes-holographic.yaml) | Hermes holographic-memory benchmark preset. |
| `hermes-honcho` | `canonical` | `active` | `hermes` | `hermes-honcho` | [`canonical/hermes-honcho.yaml`](../../config/systems/canonical/hermes-honcho.yaml) | Hermes Honcho-backed benchmark preset. |
| `hermes-hindsight` | `canonical` | `active` | `hermes` | `hermes-hindsight` | [`canonical/hermes-hindsight.yaml`](../../config/systems/canonical/hermes-hindsight.yaml) | Hermes Hindsight-backed benchmark preset. |
| `openclaw` | `canonical` | `active` | `openclaw` | `openclaw` | [`canonical/openclaw.yaml`](../../config/systems/canonical/openclaw.yaml) | Default hybrid OpenClaw benchmark preset. |
| `openclaw-fts` | `canonical` | `active` | `openclaw` | `openclaw-fts` | [`canonical/openclaw-fts.yaml`](../../config/systems/canonical/openclaw-fts.yaml) | OpenClaw full-text retrieval preset. |
| `openclaw-fts-noflush` | `canonical` | `active` | `openclaw` | `openclaw-fts-noflush` | [`canonical/openclaw-fts-noflush.yaml`](../../config/systems/canonical/openclaw-fts-noflush.yaml) | OpenClaw full-text preset without explicit flushes. |
| `openclaw-hybrid-noflush` | `canonical` | `active` | `openclaw` | `openclaw-hybrid-noflush` | [`canonical/openclaw-hybrid-noflush.yaml`](../../config/systems/canonical/openclaw-hybrid-noflush.yaml) | OpenClaw hybrid preset without explicit flushes. |
| `openclaw-vector` | `canonical` | `active` | `openclaw` | `openclaw-vector` | [`canonical/openclaw-vector.yaml`](../../config/systems/canonical/openclaw-vector.yaml) | OpenClaw vector retrieval preset. |
| `openclaw-vector-noflush` | `canonical` | `active` | `openclaw` | `openclaw-vector-noflush` | [`canonical/openclaw-vector-noflush.yaml`](../../config/systems/canonical/openclaw-vector-noflush.yaml) | OpenClaw vector preset without explicit flushes. |
| `openclaw-docker` | `canonical` | `active` | `openclaw-docker` | `openclaw-docker` | [`canonical/openclaw-docker.yaml`](../../config/systems/canonical/openclaw-docker.yaml) | Default containerized OpenClaw benchmark preset. |
| `openclaw-docker-evermemos` | `canonical` | `active` | `openclaw-docker` | `openclaw-docker-evermemos` | [`canonical/openclaw-docker-evermemos.yaml`](../../config/systems/canonical/openclaw-docker-evermemos.yaml) | Containerized OpenClaw with the EverMemOS plugin. |
| `openclaw-docker-mem0` | `canonical` | `active` | `openclaw-docker` | `openclaw-docker-mem0` | [`canonical/openclaw-docker-mem0.yaml`](../../config/systems/canonical/openclaw-docker-mem0.yaml) | Containerized OpenClaw with the Mem0 plugin. |
| `openclaw-docker-memcore-session-bundle` | `canonical` | `active` | `openclaw-docker` | `openclaw-docker-memcore-session-bundle` | [`canonical/openclaw-docker-memcore-session-bundle.yaml`](../../config/systems/canonical/openclaw-docker-memcore-session-bundle.yaml) | Containerized OpenClaw Memcore session-bundle preset. |
| `openclaw-docker-openviking-session-bundle-memcore` | `canonical` | `active` | `openclaw-docker` | `openclaw-docker-openviking-session-bundle-memcore` | [`canonical/openclaw-docker-openviking-session-bundle-memcore.yaml`](../../config/systems/canonical/openclaw-docker-openviking-session-bundle-memcore.yaml) | OpenViking session bundle with Memcore retrieval. |
| `openclaw-docker-openviking-session-bundle-noop` | `canonical` | `active` | `openclaw-docker` | `openclaw-docker-openviking-session-bundle-noop` | [`canonical/openclaw-docker-openviking-session-bundle-noop.yaml`](../../config/systems/canonical/openclaw-docker-openviking-session-bundle-noop.yaml) | OpenViking session bundle with no-op memory retrieval. |
| `hermes` | `alias` | `compatibility` | `hermes` | `hermes-holographic` | — | Compatibility alias for Hermes holographic memory. |
| `openclaw-hybrid` | `alias` | `compatibility` | `openclaw` | `openclaw` | — | Compatibility alias for the default hybrid OpenClaw preset. |
| `openclaw-agent-local` | `experiment` | `experimental` | `openclaw` | `openclaw-agent-local` | [`experiments/openclaw-agent-local.yaml`](../../config/systems/experiments/openclaw-agent-local.yaml) | Experimental agent-local OpenClaw retrieval preset. |
| `openclaw-hypercompositor` | `experiment` | `experimental` | `openclaw` | `openclaw-hypercompositor` | [`experiments/openclaw-hypercompositor.yaml`](../../config/systems/experiments/openclaw-hypercompositor.yaml) | Experimental OpenClaw HyperCompositor preset. |
| `openclaw-native-embed` | `experiment` | `experimental` | `openclaw` | `openclaw-native-embed` | [`experiments/openclaw-native-embed.yaml`](../../config/systems/experiments/openclaw-native-embed.yaml) | Experimental native OpenClaw embedding preset. |
| `openclaw-native-noembed` | `experiment` | `experimental` | `openclaw` | `openclaw-native-noembed` | [`experiments/openclaw-native-noembed.yaml`](../../config/systems/experiments/openclaw-native-noembed.yaml) | Experimental native OpenClaw preset without embeddings. |
| `openclaw-noop` | `experiment` | `experimental` | `openclaw` | `openclaw-noop` | [`experiments/openclaw-noop.yaml`](../../config/systems/experiments/openclaw-noop.yaml) | Experimental no-op OpenClaw memory preset. |
| `openclaw-docker-hypercompositor` | `experiment` | `experimental` | `openclaw-docker` | `openclaw-docker-hypercompositor` | [`experiments/openclaw-docker-hypercompositor.yaml`](../../config/systems/experiments/openclaw-docker-hypercompositor.yaml) | Experimental containerized HyperCompositor preset. |
| `openclaw-docker-memclaw` | `experiment` | `experimental` | `openclaw-docker` | `openclaw-docker-memclaw` | [`experiments/openclaw-docker-memclaw.yaml`](../../config/systems/experiments/openclaw-docker-memclaw.yaml) | Experimental containerized MemClaw preset. |
| `openclaw-docker-stub` | `experiment` | `experimental` | `openclaw-docker` | `openclaw-docker-stub` | [`experiments/openclaw-docker-stub.yaml`](../../config/systems/experiments/openclaw-docker-stub.yaml) | Experimental stub-memory preset; its image may need to be built locally. |
| `openclaw-docker-memcore-session-bundle-emptytail` | `ablation` | `experimental` | `openclaw-docker` | `openclaw-docker-memcore-session-bundle-emptytail` | [`ablations/openclaw-docker-memcore-session-bundle-emptytail.yaml`](../../config/systems/ablations/openclaw-docker-memcore-session-bundle-emptytail.yaml) | Memcore session-bundle empty-tail ablation. |
| `openclaw-docker-memcore-session-bundle-weaktail` | `ablation` | `experimental` | `openclaw-docker` | `openclaw-docker-memcore-session-bundle-weaktail` | [`ablations/openclaw-docker-memcore-session-bundle-weaktail.yaml`](../../config/systems/ablations/openclaw-docker-memcore-session-bundle-weaktail.yaml) | Memcore session-bundle weak-tail ablation. |
| `openclaw-docker-openviking-session-bundle-noop-fixpack` | `ablation` | `experimental` | `openclaw-docker` | `openclaw-docker-openviking-session-bundle-noop-fixpack` | [`ablations/openclaw-docker-openviking-session-bundle-noop-fixpack.yaml`](../../config/systems/ablations/openclaw-docker-openviking-session-bundle-noop-fixpack.yaml) | OpenViking no-op session-bundle fix-pack ablation. |
| `openclaw-docker-openviking-session-bundle-noop-serial` | `tooling` | `experimental` | `openclaw-docker` | `openclaw-docker-openviking-session-bundle-noop-serial` | [`tooling/openclaw-docker-openviking-session-bundle-noop-serial.yaml`](../../config/systems/tooling/openclaw-docker-openviking-session-bundle-noop-serial.yaml) | Diagnostic-only serialized OpenViking no-op preset for QA attribution; not suitable for benchmark scoring. |

## Inheritance rules

A leaf may declare `extends: "_bases/<name>.yaml"`. Inheritance recursively
merges mappings; lists and scalar values replace their parent value. Put a
field in a base only when every leaf has that field with the same complete
value. Optional keys must remain absent from leaves that did not define them.
Keep each family to the shortest useful source chain, normally one base and
one categorized leaf.

The session-bundle family starts with the two bases
`_bases/openclaw-memcore-session-bundle.yaml` and
`_bases/openclaw-openviking.yaml`. See the [OpenViking operational
notes](openviking.md) before changing the OpenViking leaves.

The three memcore leaves intentionally differ only in
`openclaw.ingest_session_tail`. The emptytail value `""` explicitly disables
the tail; it does not fall back to an adapter default.

## Secrets and paths

Commit only environment markers such as `${LLM_API_KEY}`; never put a secret
value in a system YAML, fixture, result, or document. Runtime secret names must
be explicitly allowlisted where a container needs them.

Every registry `path` and `extends` target is a relative POSIX path contained
under `evaluation/config/systems/`. Absolute paths, `..`, backslashes, symlink
traversal, and direct extension of an alias are rejected by the loader.
