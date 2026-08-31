# Local discovery report contract

The local discovery report is a private, content-free summary format for a
bounded compatibility or readiness pass. It is designed to make availability
and provenance explicit before anyone interprets a measure as a behavioural
finding.

## Scope

Version 1 supports exactly one developer, one repository, and one source
client in a report. It is not representative of an organisation and cannot
qualify a candidate for cross-repository evaluation by itself.

## Report contents

Each declared diagnostic has one result for every selected session and one
aggregate result. A result is either computed, unavailable, or not reportable;
computed values include an explicit provenance basis and calculation-rule
version.

The report also records reconciliation availability separately from behavioural
diagnostics. Reconciliation is an adapter-integrity measure, not evidence of
workflow quality.

## Privacy and integrity

The format contains hashes and synthetic short session labels only. It does not
permit raw trace text, workspace paths, direct session identifiers, tool
arguments, tool outputs, or personal identifiers.

Writing is exclusive and atomic: a report identifier cannot silently overwrite
an existing report. The report validator checks scope, coverage, declared
diagnostic vocabulary, availability reconciliation, and provenance shape before
the report is accepted.

## Interpretation boundary

The report is a technical readiness artifact. A potentially useful signal must
still enter the separate pattern-selection process with its own observable
definition, confounds, quality/safety oracle, and stopping rule. A small local
report cannot substitute for that process.
