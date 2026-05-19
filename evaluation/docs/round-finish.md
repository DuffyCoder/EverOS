# LoCoMo Round Finish — Archive & Reset

`evaluation/scripts/round_finish.sh` is a manual post-round utility for
LoCoMo evaluations using the OpenClaw + OpenViking stack. It addresses
two issues:

1. **Cross-round contamination.** OpenViking data lives in the
   `openviking` container's writable layer (`/app/data/`, ~65 MB).
   The host mount (`/Data/.../.openviking`) only holds config. Conv
   containers are kept across rounds (`remove_container_on_stop: false`).
   Without explicit cleanup, every new round inherits the previous
   round's fake-2026 events and dangling vectordb entries.
2. **No snapshot before reset.** Cleaning the environment loses the
   evidence that explains why a round scored what it scored.

This script lets you, after a round finishes, optionally **archive**
the current OV server state + the matching `evaluation/results/...`
directory, and optionally **reset** the environment to a clean slate.

## Usage

```bash
# Snapshot + clean (typical end-of-round, auto-pick latest results dir)
bash evaluation/scripts/round_finish.sh --archive --reset

# Snapshot only (auto)
bash evaluation/scripts/round_finish.sh --archive

# Explicit NAME — useful when you ran multiple systems and want a specific one
bash evaluation/scripts/round_finish.sh --archive fix3-fix4-full-locomo10 --reset

# Just clean (no snapshot)
bash evaluation/scripts/round_finish.sh --reset
```

### How `--archive` resolves the target

- **No NAME**: pick the most recently modified `evaluation/results/locomo-*/`
  dir, derive the archive name from its basename (with leading `locomo-`
  stripped). The detected dir is printed to the log so you can ctrl-C if
  it's not the right one.
- **With NAME**: glob `evaluation/results/locomo-*-NAME/`. `NAME` usually
  matches the `--run-name` you passed to `evaluation.cli`. If no dir
  matches, the script still archives `ov_data.tar.gz` and logs a "skipping
  results copy" warning (won't fail silently).

The script appends a timestamp to the archive dir, so re-running with the
same NAME is safe — each archive lands in its own dir.

## What `--archive NAME` saves

Output: `evaluation/archives/NAME-YYYYMMDD-HHMMSS/`

```
NAME-20260519-153012/
├── manifest.txt        # round_name, archived_at, git_sha, sizes, host
├── ov_data.tar.gz      # tar of OV container's /app/data
│                       #   (viking/ namespace + vectordb/ + _system/)
└── results/            # copy of evaluation/results/locomo-*-NAME/
                        #   (eval_results.json, answer_results, checkpoint,
                        #    artifacts/.../conversations/locomo_* workspaces)
```

`results/` is a copy (`cp -rL`), so the archive stays self-contained
even if you later delete or rerun in the original `evaluation/results/`.

Conv container workspaces are bind-mounted into
`evaluation/results/.../artifacts/openclaw/run-*/conversations/locomo_*/`,
so they're already captured by the `results/` copy — no extra step.

To keep the snapshot consistent, the script stops the `openviking`
container before `docker cp`-ing the data out, then restarts it.
Downtime is ~10-15 s. Don't run this while another round is in progress.

## What `--reset` does

- Inside the `openviking` container: `rm -rf /app/data/viking/default/user/*`
  and `rm -rf /app/data/vectordb/*`, then `docker restart openviking`
  and wait for `/health` to return 200.
- On the host: `docker rm -f` every container whose image matches
  `$CONV_IMAGE_PREFIX` (default: `ghcr.io/duffycoder/openclaw-eval-plugins`).

After reset, OV's user namespace is empty (only the auto-created
`default` shows up after the restart) and no conv containers remain.

## Inspecting an archive

```bash
ARCHIVE=evaluation/archives/NAME-20260519-153012

# Overview
cat $ARCHIVE/manifest.txt
ls -la $ARCHIVE/

# OV tar: list files without extracting
tar -tzf $ARCHIVE/ov_data.tar.gz | head -30
tar -tzf $ARCHIVE/ov_data.tar.gz | grep "events/2026"     # fake events
tar -tzf $ARCHIVE/ov_data.tar.gz | awk -F/ '{print $2}' | sort -u

# Read one file out of the tar without unpacking the whole archive
tar -xzOf $ARCHIVE/ov_data.tar.gz \
  viking/default/user/locomo_0/memories/events/2026/05/18/melanie_charity_race.md

# Full unpack (when you want to grep around)
mkdir -p /tmp/ov-inspect && tar -xzf $ARCHIVE/ov_data.tar.gz -C /tmp/ov-inspect
ls /tmp/ov-inspect/viking/default/user/

# Results dir is plain — read directly
cat $ARCHIVE/results/locomo-*/eval_results.json | jq '.accuracy, .correct, .total_questions'
```

## Environment overrides

| Env var | Default | What it controls |
|---|---|---|
| `OV_CONTAINER` | `openviking` | OV container name |
| `OV_HEALTH_URL` | `http://127.0.0.1:1933/health` | URL polled after OV restart |
| `CONV_IMAGE_PREFIX` | `ghcr.io/duffycoder/openclaw-eval-plugins` | Image prefix used to select conv containers to remove |

## Not in scope

Out of scope for this script (do them manually if needed): comparing two
archives, restoring an archive back into OV, archiving docker logs.
