# Local Evaluation Analysis

This directory is the local workspace for human-authored evaluation analysis.
Analysis reports are intentionally excluded from Git. Only this policy file and
[`TEMPLATE.md`](TEMPLATE.md) are versioned.

The existing [`round_finish.sh` procedure](../../../evaluation/docs/round-finish.md)
creates a legacy full snapshot with `cp -rL`; it is not the recommended compact
archive workflow. The copied OpenClaw workspaces can be very large and may
contain configuration, authentication material, API tokens, or other secrets.
Until compact archive tooling is available, assemble the minimum evidence set
manually, keep it local, and inspect and redact every artifact before sharing.

## Workflow

1. Copy `TEMPLATE.md` to a descriptive local filename such as
   `2026-07-15-locomo-openviking-ab.md`.
2. Record the exact run identifier, source commit, resolved configuration path,
   runtime or image versions, and archive location.
3. Put raw PDFs, supporting data, manifests, and other source evidence under
   `evaluation/archives/<run-id>/`.
4. Record checksums for every artifact used to support the report. Prefer
   SHA-256 and keep the checksum manifest inside the ignored archive.
5. Confirm the report and archive are ignored before adding unrelated changes.

```bash
git check-ignore docs/evaluation/analysis/<report>.md
git check-ignore evaluation/archives/<run-id>/manifest.sha256
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
