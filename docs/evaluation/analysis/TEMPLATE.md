# Evaluation Analysis: `<short title>`

> Local report template. Copy this file to a new Markdown report in this
> directory. The copied report is ignored by Git.

## Run Identity

| Field | Value |
| --- | --- |
| Run ID | `<dataset>-<system>-<run-name>` |
| Run date | `<YYYY-MM-DD>` |
| Dataset | `<dataset and version>` |
| System | `<system and version>` |
| Configuration | `<resolved config path>` |
| Source commit | `<full Git commit SHA>` |
| Runtime/image | `<runtime and immutable image identifier>` |
| Archive | `evaluation/archives/<run-id>/` |
| Status | `<complete, partial, or failed>` |

## Question and Method

State the hypothesis, comparison, dataset scope, and any intentional deviations
from the standard evaluation protocol.

## Results

Record final metrics and the smallest amount of context needed to interpret
them. Link metrics to filenames inside the local archive rather than pasting
large raw outputs.

## Findings

Describe the conclusions, uncertainty, regressions, and follow-up work. Separate
observations from inferences.

## Errors and Limitations

List failed stages, retries, exclusions, judge limitations, data gaps, and other
conditions that affect interpretation.

## Reproduction

Record the command shape, required non-secret environment variable names,
runtime prerequisites, and restoration steps. Never include credential values.

## Evidence Checksums

| Artifact | SHA-256 | Purpose |
| --- | --- | --- |
| `<relative archive path>` | `<sha256>` | `<why this file is retained>` |

Checksum manifest: `evaluation/archives/<run-id>/manifest.sha256`
