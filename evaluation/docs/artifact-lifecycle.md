# Evaluation Artifact Lifecycle

The artifact hygiene CLI provides conservative, read-only inventory and an
explicitly gated compact archive operation. It never deletes a result and never
rewrites an archive source.

## Inventory decisions

Inventory decisions are hints for human review, not deletion authorization.
They use this precedence:

1. `keep`: the result is named in `evaluation/archives/KEEP`.
2. `keep`: a local analysis report references the result.
3. `keep`: reliable metadata explicitly establishes that the result is the
   newest successful full run for its dataset/system/config group.
4. `delete_candidate`: the result name contains, case-insensitively, one of
   `smoke`, `debug`, `retry`, `calib`, `test`, or `incomplete`.
5. `review`: no higher-confidence rule applies.

An ordinary result with unconfirmed status is always `review`; it is never
promoted automatically to `delete_candidate`. Every result contains a
machine-readable `reasons` list explaining its decision.

The current result directories do not expose reliable dataset/system/config
group metadata. Consequently, inventory never infers `is_latest_success` from
one global modification-time ordering. A successful but otherwise unpinned and
unreferenced result remains `review`. Until trustworthy grouping metadata is
available, protect the latest successful full run for each group with an
explicit KEEP entry.

Run an inventory only with an explicit output path:

```bash
python -m evaluation.tools.artifact_hygiene inventory \
  --repo-root . \
  --output /path/to/artifact-inventory.json
```

The command reports registered Git worktrees, environment-backup metadata,
results, existing archives, local analysis files, and parsed KEEP entries. It
does not edit those sources. Git discovery uses `git worktree list --porcelain`;
if Git is unavailable, the inventory contains an empty worktree list and an
error instead of failing unsafely.

Analysis inventory lists metadata for every file, but reference discovery reads
content only from regular files whose suffix is `.md` (case-insensitive). Each
Markdown file is scanned in fixed-size blocks up to 256 KiB and reports a
`reference_scan` status of `complete`, `truncated`, or `error`; non-Markdown and
non-regular entries report `not_scanned`. The scanner never loads a PDF, binary,
or arbitrarily large Markdown file into memory. If any Markdown reference scan
is truncated or fails, a name-based `delete_candidate` is downgraded to
`review` with reason `analysis_reference_scan_incomplete`, because an unseen
reference may exist beyond the scanned prefix.

Environment backups matching `.env.bak*` at the repository root or a shallow
child are represented only by path, size, mode, modification time, and type.
The tool never reads backup content and never emits a content hash. This rule
also applies when a backup has mode `000`.

## Explicit KEEP pins

`evaluation/archives/KEEP` is optional. Each non-comment, non-empty line is one
repository-relative result or artifact path:

```text
# Baseline used by the July comparison
evaluation/results/locomo-openviking-baseline
```

Absolute paths and paths containing `..` are ignored. Duplicate normalized
entries collapse to one entry. A KEEP entry matching a result, or a path below
that result, pins the whole result as `keep`.

## Compact archives

The compact package retains small, auditable evidence while preserving its
layout relative to the result directory. Eligible evidence includes:

- top-level metrics, summary, diagnostics, latency, and content-overlap JSON;
- resolved configuration in JSON, YAML, or YML;
- Markdown or text reports and logs;
- `answer*`, `search*`, and `eval*` JSON evidence; and
- JSON, text, log, YAML, YML, Markdown, or CSV evidence below `artifacts/`.

The planner excludes every other file with a machine-readable reason. It does
not follow symlinks and excludes compiled `.pyc` and `.pyo` files. It prunes
large or reproducible trees at the directory boundary, including
`openclaw-workspaces`, `node_modules`, `.cache`, `.venv`, `__pycache__`, vector
indexes, BM25 indexes, and model caches. A pruned directory receives one
directory-level exclusion; its potentially large contents are not traversed.

Archive is dry-run by default:

```bash
python -m evaluation.tools.artifact_hygiene archive \
  --result evaluation/results/<run-id> \
  --archive-root evaluation/archives
```

The command prints a JSON plan, including a deterministic `plan_digest`, to
standard output and creates nothing. A later invocation regenerates a fresh
in-memory plan from the source as it exists at execution time. Bind execution
to the reviewed path/size/reason selection by passing that digest. A mismatch
fails before the archive root is created:

```bash
python -m evaluation.tools.artifact_hygiene archive \
  --result evaluation/results/<run-id> \
  --archive-root evaluation/archives \
  --expected-plan-digest <reviewed-plan-digest> \
  --execute
```

Execution creates a private, uniquely named staging directory with mode `0700`
under the held archive-root directory. Source and destination paths are opened
component by component with directory file descriptors, no-follow flags, and
nonblocking final-file opens. The copier rejects a symlinked ancestor, a FIFO
or other special file, a size mismatch against the reviewed plan, or a source
whose device, inode, mode, size, modification time, or change time moves during
copy. All writes remain relative to held directory descriptors; a path
containment check is never treated as the write boundary.

The staging tree contains only manifest-listed regular files, their required
parent directories, and `manifest.json`. Immediately before publication the
tool walks that exact tree again without following symlinks, rejects extra or
missing entries, and rechecks every size and SHA-256 (including the manifest).
It also reopens the canonical source and archive-root paths and confirms that
their directory identities still match the held descriptors. Any failure
before publication removes only the inode-verified staging directory, leaving
no final archive and allowing a clean retry.

Publication uses Linux/POSIX `renameat2(RENAME_NOREPLACE)`: an existing final
directory, including an empty one created concurrently, is never replaced.
Platforms without secure POSIX dirfd operations or atomic no-replace rename
fail closed. After the atomic rename, an archive-root `fsync` failure is
reported explicitly as "published ... uncertain crash durability"; at that
point the complete final archive exists and is not misidentified or removed as
staging. A successful `manifest.json` contains relative paths, sizes,
checksums, reasons, the source, archive timestamp, and Git SHA. If a Git SHA
cannot be determined, it records `unknown` and the lookup error. Review that
manifest against the preview before using or sharing the archive. The operation
never deletes or modifies the source.

The plan digest binds selection metadata, not file contents. Quiesce the result
directory before preview and execution; execution rejects files that change
while being copied, and the manifest SHA-256 values remain the authority for
the bytes actually copied. A same-size replacement completed before execution
is not detected by the plan digest, but its actual bytes are hashed into the
manifest and verified again before publication. Omitting
`--expected-plan-digest` retains the execute gate but does not bind it to an
earlier dry-run.

## Restore and review checks

Before using an archive for reproduction or review:

1. Confirm the manifest schema, source run identifier, archive timestamp, and
   Git SHA (or its recorded `unknown` explanation).
2. Reject absolute included paths and any path containing `..`.
3. Recompute SHA-256 for every included regular file and compare its size and
   digest with `manifest.json`.
4. Restore into a separate scratch directory. Check out the recorded commit and
   review the resolved configuration before running anything.
5. Regenerate excluded workspaces, indexes, dependencies, and caches from
   trusted inputs; do not treat them as missing archive data.
6. Inspect evidence for credentials or sensitive data before sharing it.

Deletion is a separate lifecycle action. It must be authorized by a separately
generated deletion manifest that a human has reviewed against the current
inventory, KEEP pins, analysis references, successful-run status, and archive
checksums. This CLI deliberately has no `delete` subcommand; neither
`delete_candidate` nor an archive manifest grants deletion authority.
