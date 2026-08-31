# Privacy model

Agent Policy Lab is designed on the premise that coding-agent traces can
contain prompts, source paths, tool arguments, tool output, credentials, and
other sensitive workspace context. The default is local handling, minimal
normalisation, and no implicit publication path.

## Two-tier boundary

**Tier 1** is a protected local store for raw traces and working artifacts. It
is outside the source repository and is the only place where a trace is read.

**Tier 2** contains synthetic fixtures or screened derived artifacts. It may be
shared only after the export gate validates the source run, screens the output,
and records required human sign-off.

## Data minimisation

Adapters retain only fields required by the declared contract. Where a client
does not expose a value safely and reliably, the field is `unavailable`; the
adapter does not invent a substitute.

Examples of data that must not appear in a shareable artifact include raw
message text, thinking content, tool arguments, tool outputs, raw source
references, filesystem paths, secrets, and direct session identifiers.

## No individual performance inference

Usage, duration, tool count, compaction, and explicit tool-result states are
operational signals. None is a productivity, quality, or compliance score for
a person. Any owner-facing question must state its evidence source, confounds,
and what the measure cannot establish.

## Deployment boundary

Using real traces requires explicit governance for source scope, consent,
access control, retention, deletion, and the exact aggregate outputs permitted
to leave the local environment. This repository provides technical controls and
reference patterns; it does not grant collection authority.

At organisational scale, this means the shared corpus is formed from approved
Tier 2 reports, not by centralising Tier 1 traces. A report must state its
cohort, calculation rule, availability and exclusions so an aggregate does not
silently turn a client limitation into a behavioural conclusion. See [the
organisation learning model](organisation-learning-model.md) for the full
operating model.
