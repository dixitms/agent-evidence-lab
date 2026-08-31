# Compaction continuity review

Context compaction discards earlier conversation detail so a session can carry
on. Compaction continuity is a candidate diagnostic for one specific failure
mode afterwards: the agent resumes an earlier, already-superseded task instead
of continuing the instruction that was actually current.

## The boundary this toolkit enforces

Canonical events can say *where* to look. They cannot say what happened.

The extractor finds compaction events in one canonical session and builds a
small pre/post event window around each. A window holds event identifiers,
their order, and an eligibility reason — no message content, no tool arguments,
no tool output, no paths. Nothing in that window establishes that the agent
lost track of anything, and the extractor never claims it did.

The classification is a separate act: a local human reads the protected
pre/post context and records an outcome. Only that review may apply a label.

## When a window is worth reviewing

A candidate is annotation-eligible only when all of the following hold:

- The boundary is an **observed `boundary_marker`** compaction event. A
  continuation summary, an uncharacterised source, or an adapter-synthesized
  record may well describe a real compaction, but a window built on one rests
  on a boundary the client never attested. Those start out ineligible; widening
  the accepted set is a later decision that needs its own evidence.
- A **prior user-message anchor** exists in the pre-window.
- **Substantive agent activity** follows the boundary — an assistant message,
  a thinking block, or a tool call. A `tool_result` does not count: it usually
  completes a tool call issued *before* the boundary, so it evidences a
  pre-compaction decision rather than the agent's choice of what to do next.
- **No new user message** appears between the boundary and that first activity.
  A fresh instruction is a change of subject, not a continuity failure.

Both edges of the window stop at the neighbouring compaction. In particular the
pre-window is clamped after the *previous* boundary, so an instruction that had
itself already been compacted away cannot serve as the anchor for this one.

Ineligible windows are returned with an explicit reason rather than dropped.
They stay countable and inspectable; they are simply not labellable.

## What the annotation may contain

Controlled codes only. The annotation has no free-text field, and adding one
would defeat the artifact's purpose — a rationale box is where transcript
quotes end up.

Outcomes:

| Outcome | Meaning |
| --- | --- |
| `continued_current_task` | The agent carried on with the instruction that was current. |
| `resumed_superseded_task` | The agent picked up an earlier instruction that had been superseded. |
| `legitimate_revisit` | Earlier work was revisited for a good reason. |
| `unclear` | The reviewer could not determine it. |

Each outcome declares which rationale codes it **requires** and which
**contradict** it. `resumed_superseded_task` requires evidence that the latest
task was not addressed *and* that a superseded one was, and it is refused if it
also carries a revisit justification — that combination is exactly the
ambiguity this outcome has to be distinguishable from. `legitimate_revisit`
requires an actual justification: a user authorisation or a dependency.

## Identifiers

The annotation names no session and no event. It references its window through
an opaque keyed handle — an HMAC over the session identifier and the boundary
position under a local key. The handle commits to both, so an annotation cannot
be reattached to a different boundary, and it discloses neither without the
key. The candidate window object itself does hold the local session and event
identifiers, because a reviewer has to find the events on their own machine;
those never reach the annotation that outlives the review.

## Tier 1 terminal

A continuity annotation is **never exportable**. It records a human's reading
of protected transcript context, and "a reviewer concluded the agent resumed a
superseded task" is itself a claim about a private session. There is no
screened derivative of it and no export form — the export gate refuses an
embedded annotation structurally rather than redacting it.

Aggregate counts derived from many annotations are a separate question with its
own evidence requirements, and are not authorised by this contract.

## Evaluation path

Several locally reviewed incidents may justify a separate controlled study. A
candidate intervention could be a post-compaction recovery skill or a
structured continuation summary. Its quality oracle must verify that the final
result satisfies the latest task and preserves required behaviour; merely
following a checkpoint is not evidence of quality.
