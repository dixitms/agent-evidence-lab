# Reference case study: a seeded policy loop

This reference case study tested whether the project could complete a bounded
evaluation loop from trace normalisation to a policy decision.

## Design

The study used three narrow, synthetic coding tasks on a pinned public
benchmark repository. It compared a baseline with a repository-local policy
intervention. A preregistered detector measured a repeated-read pattern, while
a separate task-specific oracle required a correct patch, regression tests, and
the benchmark suite floor.

## Result

The task-quality oracle passed for all analysed sessions. The repeated-read
pattern appeared in neither baseline nor intervention sessions. The evidence
therefore did not permit a claim that the policy helped, harmed, or had no
effect.

The policy-effect decision was **unassessable**: the study was valid for
testing the apparatus, but its intended behavioural branch did not activate.

## What this establishes

- A trace can be normalised, reconciled, validated, and screened through the
  local-first pipeline.
- A compliance measure and a task-quality oracle can be kept separate.
- A non-result can be retained without retrofitting the task or detector to
  manufacture activation.

## What this does not establish

- Prevalence of the pattern across repositories, users, models, or clients.
- Causal effectiveness of the intervention.
- A general claim about agent quality or developer workflow.

The next credible step for a new pattern is discovery in an appropriately
governed local cohort, followed—only if the signal is observable and
actionable—by a separately preregistered confirmation in a public benchmark or
approved sandbox.
