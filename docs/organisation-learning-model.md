# Organisation learning model

This reference implementation supports a way for an engineering organisation
to learn from coding-agent use without centralising developer conversations or
turning trace data into an individual-performance system.

The practical objective is:

> Across approved environments, where are agent capabilities, configuration,
> policies, and workflows creating avoidable friction or a meaningful
> safety/quality gap—and what small, reversible improvement should an owner
> test next?

The output is organisational knowledge: a configuration repair, capability
rollout, skill, runbook, evaluation method, or decision to stop. It is not a
ranking of developers or a claim that more tokens, longer sessions, or more
tool calls mean better or worse work.

## The distributed evidence corpus

Raw traces can contain prompts, code, paths, credentials, tool arguments, and
tool output. They remain in a protected Tier 1 store in the approved local
environment that produced them. There is no central raw-trace warehouse in
this model.

Each local environment may instead emit a permitted Tier 2 report. These
reports form the organisation's evidence corpus. A report is deliberately
small and must carry enough context to be interpreted honestly:

- the approved cohort and trace profile;
- the client and schema version;
- the calculation rule and its version;
- field availability and exclusions;
- whether a quantity is a canonical field, a derived measure, or an approved
  external source; and
- what the result cannot establish.

For example, a report may say that an explicitly observed tool-result failure
rate rose after a particular client configuration changed. It cannot claim that
the configuration made developers less productive, that every failed tool call
was harmful, or that an individual developer caused the difference.

## Operating sequence

```text
approved local trace
  → local normalisation and reconciliation
  → screened Tier 2 coverage/diagnostic report
  → aggregate owner question
  → bounded investigation
  → reversible improvement
  → independent quality and safety check
  → keep / revise / retire / unassessable
```

### 1. Define the local collection boundary

Before any real trace is read, the responsible owner defines the approved
source type, cohort, access model, retention period, deletion process, and
which Tier 2 outputs may leave the environment. Approval is specific: approval
for session traces does not automatically authorise configuration files, source
control history, CI data, issue trackers, or message content.

### 2. Prove trace compatibility locally

An adapter first establishes what the client actually records: record kinds,
ordering, pairing, session boundaries, and field availability. It reconciles
its canonical output with the same immutable snapshot of the raw input. An
unknown or incomplete shape is preserved or refused; it is not approximated.

### 3. Produce a minimal Tier 2 report

Local computation produces only approved, screened results. Suitable early
examples include coverage of compaction markers, assistant token-use fields,
explicit tool-result outcomes, tool-call distribution, or reconciliation
success. They are operational signals, not quality scores.

If a client omits a field—for example, a result outcome—the report records it
as unavailable. Missing data is never counted as a favourable result.

### 4. Select an owner-valued question

An aggregate is useful only if someone can act on it. The question should name
an owner and a plausible, low-risk improvement. Examples:

- Are teams missing an approved capability because it is inconsistently
  configured across managed environments?
- Does a workflow create repeated explicit failures that a better skill or
  runbook could prevent?
- Does a compaction boundary coincide with an increase in a clearly scoped,
  observed failure signal that warrants a local continuity review?

If no owner-valued question emerges, stop. Collecting more data is not itself a
success condition.

### 5. Investigate at the smallest justified scope

Some questions can be answered from structural events alone. Others need a
local reviewer to inspect protected context, for example to decide whether an
agent resumed an obsolete task after compaction. That review remains Tier 1.
It should use controlled outcomes and avoid exporting transcript excerpts or
free-text conclusions.

A semantic or content-based question requires separate explicit approval. It
must not be smuggled into a structural metric by calling a proxy “agent
quality.”

### 6. Test a reversible improvement separately

Do not treat a descriptive report as evidence that a policy works. Freeze a
separate evaluation plan: the pattern definition, eligible cohort, proposed
improvement, quality or safety oracle, stopping rule, and interpretation
limits. Use a public benchmark or separately approved sandbox where possible,
rather than a developer's live workspace.

The improvement may be a capability/configuration repair, retrieval or
navigation aid, skill, runbook, advisory policy, or only as a last resort a
workflow guardrail. Its quality check must test the work outcome independently
of whether the agent followed the intervention.

## Roles and accountability

| Role | Accountable for |
| --- | --- |
| Environment or tooling owner | Source boundary, configuration, rollout, and operational response |
| Data/governance owner | Consent, access, retention, deletion, and permitted Tier 2 outputs |
| Evaluation owner | Measurement rule, confounds, quality/safety oracle, and stopping rule |
| Local reviewer, where needed | Tier 1 contextual classification without exporting protected content |
| Improvement owner | A reversible change and its decision: keep, revise, retire, or unassessable |

No role is assigned to judge individual developer performance from the
evidence corpus.

## Stop signals

The model deliberately fails fast:

- If the trace cannot support an honest measure, record the measure as
  unavailable and do not substitute a weaker proxy.
- If reconciliation fails, do not aggregate the output.
- If a report lacks approved provenance, cohort, or exclusions, do not share
  it.
- If no owner can act on a pattern, stop rather than expanding collection.
- If an intervention never activates the pattern or cannot be assessed fairly,
  record it as unassessable rather than claiming success or failure.

This makes the hill-climbing loop evidence-led: observations select the next
small experiment; results decide whether to retain, change, or remove an
improvement. The loop optimises the environment and its safeguards, not the
people working within it.
