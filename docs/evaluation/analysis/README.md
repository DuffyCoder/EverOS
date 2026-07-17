# Local Evaluation Analysis

This directory is the local workspace for human-authored evaluation analysis.
Analysis reports are intentionally excluded from Git. Only this policy file and
[`TEMPLATE.md`](TEMPLATE.md) are versioned.

The existing [`round_finish.sh` procedure](../../../evaluation/docs/round-finish.md)
creates a legacy full snapshot with `cp -rL`; it is not the recommended compact
archive workflow. The copied OpenClaw workspaces can be very large and may
contain configuration, authentication material, API tokens, or other secrets.
For result-directory evidence, use the implemented
[artifact lifecycle workflow](../../../evaluation/docs/artifact-lifecycle.md):
preview a compact archive, review its deterministic plan digest, and bind
execution to that digest. Raw PDFs and external supporting data still require
manual curation because they are outside that result-evidence planner; keep
only the minimum needed, keep it local, checksum it, and inspect and redact it
before sharing.

## Workflow

1. Copy `TEMPLATE.md` to a descriptive local filename such as
   `2026-07-15-locomo-openviking-ab.md`.
2. Record the exact run identifier, source commit, resolved configuration path,
   runtime or image versions, and archive location.
3. Quiesce the result directory and keep it quiescent through execution. Then
   preview compact evidence. This is a dry run and creates nothing:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run python \
     -m evaluation.tools.artifact_hygiene archive \
     --result evaluation/results/<run-id> \
     --archive-root evaluation/archives
   ```

4. Inspect every inclusion, exclusion reason, path, and size in the JSON plan.
   Copy its `plan_digest`, keep the result directory quiescent, then execute
   only with the reviewed digest:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run python \
     -m evaluation.tools.artifact_hygiene archive \
     --result evaluation/results/<run-id> \
     --archive-root evaluation/archives \
     --expected-plan-digest <reviewed-plan-digest> \
     --execute
   ```

5. Manually put only necessary raw PDFs and external supporting data in a
   separate local evidence area such as
   `evaluation/archives/<run-id>-external/`; never add them to the generated
   compact archive directory or bulk-copy source trees and caches.
6. Record SHA-256 checksums for every manual artifact used by the report and
   keep that checksum manifest inside the ignored archive. The compact
   archive's own `manifest.json` remains authoritative for files it includes.
7. Confirm the report and archive are ignored before adding unrelated changes.

```bash
git check-ignore docs/evaluation/analysis/<report>.md
git check-ignore evaluation/archives/<run-id>-external/manifest.sha256
```

Do not copy API keys, tokens, unredacted environment files, or other credentials
into a report or archive. A report may name required environment variables, but
must never include their values.

The archived evidence and local report are one unit: the report explains the
result, while the archive preserves the minimum auditable evidence needed to
reproduce or review it. Regenerable workspaces, caches, duplicate models, and
intermediate runtime files do not belong in a compact archive. A legacy full
snapshot retained for incident analysis must be labelled as such and remain
subject to the security and size warning above.
