# Architecture overview

Agent Policy Lab is built as a local-first evidence pipeline rather than a
telemetry dashboard.

```text
client trace
  → snapshot
  → client/profile adapter
  → canonical events + preserved-stream accounting
  → reconciliation and dataset validation
  → protected Tier 1 store
  → fail-closed screened export, if authorised
```

## Canonical contracts

The schemas define a common event envelope and a small family of typed
payloads: user and assistant messages, tool calls/results, thinking, and
compaction. Every payload field carries an explicit availability status:
`observed`, `inferred`, or `unavailable`.

This prevents a client with weaker telemetry from being presented as though it
had recorded a field that it did not provide.

Experiment-run records hold session/run lineage separately from individual
events. This keeps task, model, policy, condition, and disposition metadata
visible without duplicating it on every event.

## Source clients and trace profiles

Adapters are client-specific. A managed-environment deployment profile is not a
new client: it is a constrained interpretation of a known client trace shape,
with its own fail-closed rules and documented availability limits.

Before a profile can support behavioural interpretation, it needs a bounded
structural compatibility check: raw record kinds, tool pairing, ordering,
session/turn semantics, field availability, and graph integrity. Unknown raw
shapes are preserved or refused; they are never silently normalised as a known
event type.

## Snapshot and reconciliation

A source file is captured as immutable bytes with a SHA-256 hash before
normalisation. Reconciliation compares canonical class counts with the same
captured input. If a source changes between operations, the run fails rather
than comparing different file states.

Reconciliation is per class, not only a total-count check. A total can remain
unchanged while an adapter mislabels or loses an important event family.

## Storage and export

Tier 1 storage is deliberately outside the repository. It contains protected
raw snapshots, normalized events, and manifests. A run must pass schema,
dataset, manifest, and reconciliation checks together.

Public output is a screened transformation, never a raw trace with selected
fields removed. The export path validates its Tier 1 source first, then rejects
non-exportable run kinds, prohibited fields, secrets, paths, malformed
manifests, and incomplete sign-off.

## Evaluation loop

An observed pattern is not an intervention result. A candidate must be
observable, actionable, safely measurable, and paired with an independent
task-quality or safety oracle. The experiment protocol freezes its task,
detector, intervention, quality gate, eligibility rule, and stopping rule
before controlled sessions begin.

The result may support keeping, revising, retiring, or marking an intervention
unassessable. The latter is a valid outcome when the pattern never activates or
the available evidence cannot answer the policy-effect question.
