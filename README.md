# Agent Policy Lab

Agent Policy Lab is a local-first reference implementation for studying coding
agent workflows without treating raw traces as a dashboard dataset or a
developer-performance score.

It provides a disciplined loop:

```text
compatible trace → bounded signal → owner-reviewed question
                 → reversible improvement → independent quality/safety check
                 → keep / revise / retire / unassessable
```

The aim is to help engineering organisations learn where agent capabilities,
tools, policies, and workflows are inconsistently available or create
avoidable friction. The output is reusable organisational knowledge—such as a
configuration repair, skill, runbook, or evaluation method—not an individual
ranking.

## What is included

- Versioned canonical event and experiment-run schemas.
- Adapters for Claude Code and Codex CLI, plus a provisional
  managed-environment Claude Code trace profile.
- Snapshot, reconciliation, validation, and fail-closed export components.
- A content-free compaction-window extractor and local human-annotation
  contract for studying task continuity without claiming intent from telemetry.
- Synthetic fixtures and tests for normalisation, privacy boundaries, and
  error handling.
- A reference workflow for preregistered, quality-gated policy evaluation.

All committed fixtures are synthetic. Raw traces, secrets, local stores,
workspace paths, and export bundles are intentionally excluded.

## What it is not

- A monitoring system or developer-performance scorecard.
- A claim that token use, session duration, or tool volume measures quality or
  productivity.
- A central raw-trace collection service.
- A substitute for an organisation's security, privacy, or deployment
  governance.

## Local development

Create a Python virtual environment, install the project in editable mode with
its development extras, and run the test suite:

```bash
python -m pip install -e ".[dev]"
pytest -q
```

The project has no network-service dependency for its synthetic test suite.

## Privacy model

The design assumes a two-tier boundary:

- **Tier 1:** local, protected raw traces and derived working artifacts.
- **Tier 2:** screened, aggregate or synthetic artifacts suitable for sharing
  only after fail-closed validation and human sign-off.

Some artifacts are **Tier 1 terminal**: they have no screened form at all. A
compaction-continuity annotation is one, because it records a human's reading
of protected transcript context.

See [the privacy model](docs/privacy-model.md) and
[the architecture overview](docs/architecture.md) for the full approach.

## Documentation

- [Architecture overview](docs/architecture.md) — the pipeline and its contracts.
- [Privacy model](docs/privacy-model.md) — the two-tier boundary and what it forbids.
- [Compaction continuity](docs/compaction-continuity.md) — how a bounded
  candidate window is separated from the human judgment that labels it.
- [Local discovery report contract](docs/local-discovery-report-contract.md) —
  the shape of a private, single-corpus coverage report.
- [Organisation learning model](docs/organisation-learning-model.md) — how
  consented local evidence can become organisation-wide learning without a
  central raw-trace corpus.
- [Roadmap](docs/roadmap.md) — the path from a local clone to approved,
  repeatable Tier 2 reporting.
- [Reference case study](docs/case-study-seeded-policy-loop.md) — an
  end-to-end run whose policy effect was unassessable.

## Evidence posture

A small seeded reference case study demonstrated the end-to-end apparatus but
did not establish a policy effect: the preregistered behaviour did not activate
in either arm. Its policy-effect decision was therefore **unassessable**.
Negative and unassessable results are retained because they prevent convenient
stories from replacing evidence.

See [the reference case study](docs/case-study-seeded-policy-loop.md).

## Status

This is a research and reference implementation. Any deployment involving real
developer traces requires separate, explicit governance for source scope,
consent, access, retention, deletion, and permitted aggregate outputs.
