# Roadmap

This roadmap describes how the reference implementation becomes useful in an
approved managed environment. It is ordered by the evidence and safety gates
that make each capability trustworthy, rather than by dashboard features.

## Current foundation

The repository already provides:

- client and trace-profile adapters;
- immutable snapshots and per-class reconciliation;
- canonical schemas with explicit availability states;
- local Tier 1 storage and validation components;
- a fail-closed export gate; and
- synthetic fixtures that exercise normal and failure paths.

These pieces establish whether a trace can be interpreted honestly. They do
not authorise reading real traces or create a central collection service.

## Next: repeatable local Tier 2 reporting

The next product capability is an operator-facing local runner. It should turn
an approved, explicit source selection into one screened Tier 2 report.

### What the runner must do

1. Read a declared local source only. It must not discover or scan a home
   directory by default.
2. Capture immutable snapshots, normalize them with the selected adapter, and
   reconcile every supported event class against the same bytes.
3. Store raw inputs and working artifacts only in a protected Tier 1 location
   outside the repository.
4. Calculate an approved diagnostic set and attach its availability,
   exclusions, calculation rule, and trace-profile version.
5. Validate and screen the report before it can leave the local environment.
6. Require the configured human sign-off before producing a shareable output.

The first diagnostic set should remain structural and content-free: adapter
coverage, reconciliation status, compaction-marker coverage, available token
fields, explicit tool-result outcome coverage, and tool-family distribution.
It must not present missing outcomes as successful calls or interpret message
content as an agent-quality score.

### Local execution sequence

For an approved pilot, the sequence is:

```text
clone repository
  → install and run synthetic tests
  → prove the local trace profile is compatible
  → approve source scope and Tier 2 output contract
  → configure an explicit local source and protected store
  → run snapshot → adapter → reconciliation → diagnostics
  → screen and sign the Tier 2 report
```

The compatibility step is intentionally before reporting. It checks the trace
shape actually present in that environment—record kinds, ordering, tool
pairing, boundaries, and field availability. A profile that fails this step is
not silently treated as equivalent to another workstation or client version.

## Then: aggregate evidence, not raw sessions

Once more than one approved local environment can produce the same report
contract, a shared aggregate service or controlled shared location can collect
only those Tier 2 reports. Its first responsibilities are modest:

- group reports only by approved cohort labels;
- retain availability and exclusion information beside every quantity;
- show coverage and uncertainty before behavioural patterns; and
- identify owner-valued questions for investigation.

It must not accept raw traces, prompts, paths, source code, tool arguments,
tool outputs, or direct session identifiers. It must not rank individuals.

## Later: evidence-led improvements

An aggregate signal can nominate a pattern for a separate evaluation cycle.
That cycle freezes a detector, eligible cohort, reversible improvement,
independent quality or safety oracle, and stopping rule before measuring the
change. It may result in keeping, revising, retiring, or marking an
intervention unassessable.

Examples of candidate improvements include a missing capability configuration,
a navigation skill, a runbook, a safer workflow default, or a narrowly scoped
advisory policy. A blocking guardrail is a last resort and requires its own
quality and safety evidence.

## Stop signals

Stop rather than work around a negative signal:

- The trace profile cannot support the requested measure.
- Reconciliation does not account for the selected input.
- The proposed report needs fields outside its approved data boundary.
- No owner can act on the apparent pattern.
- A quality or safety oracle cannot be made fair for the proposed improvement.

These are useful outcomes. They prevent the project from accumulating data,
proxies, or interventions merely because the original question was appealing.

## Deliberate non-goals

- A default whole-machine session-log scan.
- A cloud service that receives raw developer conversations.
- Individual productivity or compliance dashboards.
- Content-based interpretation without explicit separate approval.
- Treating one client’s missing telemetry as equivalent to another client’s
  observed field.
