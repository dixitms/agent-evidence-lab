"""Privacy-bounded compaction-continuity review primitives.

Context compaction discards earlier conversation detail. A question worth
asking afterwards is whether the agent carried on with the instruction that was
actually current, or picked up an older one that had already been superseded.

This module deliberately separates two jobs:

* canonical events identify a bounded candidate window around compaction; and
* a local human annotation classifies task continuity from protected context.

No function here reads a transcript, message content, tool arguments, tool
output, or workspace state. Event sequencing alone never establishes that an
agent resumed a superseded task, so the extractor never labels one; it only
decides whether a window is worth a human's attention and says why not when it
is not.

Both the candidate window and the annotation are Tier 1 terminal artifacts.
Neither is an export input. The annotation carries no direct session or event
identifier, no free text, and no transcript excerpt; it references its window
through a keyed digest that is meaningless without the local key.
"""

from __future__ import annotations

import hmac
import json
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import jsonschema


ANNOTATION_VERSION = "1.0.0"

#: Post-boundary activity that reflects a fresh agent decision. A tool_result
#: is deliberately excluded: it very often completes a tool_call issued before
#: the boundary, so it evidences a pre-compaction decision rather than the
#: agent's choice of what to do next.
SUBSTANTIVE_EVENT_TYPES = frozenset({"assistant_message", "thinking", "tool_call"})

#: Only a directly observed boundary marker is accepted as a review candidate
#: to begin with. A continuation summary, an uncharacterised source, or an
#: adapter-synthesized record may describe a real compaction, but the window
#: built around it would rest on a boundary the client never attested. Those
#: are retained as ineligible rather than dropped, so the corpus stays visible.
ACCEPTED_COMPACTION_SOURCES = frozenset({"boundary_marker"})

INELIGIBLE_REASONS = frozenset(
    {
        "compaction_boundary_not_observed",
        "compaction_source_not_accepted",
        "no_post_boundary_substantive_activity",
        "no_prior_user_message_anchor",
        "new_user_message_before_first_substantive_activity",
    }
)

#: Each outcome states which rationale codes it requires and which contradict
#: it. Without the forbidden set, a resumed-superseded finding could carry a
#: revisit justification at the same time, which is precisely the case it must
#: be distinguishable from.
OUTCOME_RATIONALE_RULES: dict[str, dict[str, frozenset[str]]] = {
    "continued_current_task": {
        "required_all": frozenset({"latest_task_addressed"}),
        "required_any": frozenset(),
        "forbidden": frozenset(
            {
                "latest_task_not_addressed",
                "superseded_task_addressed",
                "insufficient_context",
                "ambiguous_task_state",
            }
        ),
    },
    "resumed_superseded_task": {
        "required_all": frozenset({"latest_task_not_addressed", "superseded_task_addressed"}),
        "required_any": frozenset(),
        "forbidden": frozenset(
            {
                "latest_task_addressed",
                "user_authorised_revisit",
                "dependency_requires_revisit",
                "insufficient_context",
                "ambiguous_task_state",
            }
        ),
    },
    "legitimate_revisit": {
        "required_all": frozenset(),
        "required_any": frozenset({"user_authorised_revisit", "dependency_requires_revisit"}),
        "forbidden": frozenset(
            {"latest_task_not_addressed", "insufficient_context", "ambiguous_task_state"}
        ),
    },
    "unclear": {
        "required_all": frozenset(),
        "required_any": frozenset({"insufficient_context", "ambiguous_task_state"}),
        "forbidden": frozenset(
            {
                "latest_task_addressed",
                "latest_task_not_addressed",
                "superseded_task_addressed",
                "user_authorised_revisit",
                "dependency_requires_revisit",
            }
        ),
    },
}

ANNOTATION_OUTCOMES = frozenset(OUTCOME_RATIONALE_RULES)

#: A window key must be a real local secret. A short one would make the digest
#: reversible by enumeration, since the input space is one session identifier
#: and a small integer.
MINIMUM_WINDOW_KEY_BYTES = 16


def _schema_path() -> Path:
    """Find the shipped annotation contract in a checkout or installed wheel."""
    filename = "compaction_continuity_annotation.schema.json"
    candidates = (
        Path(__file__).resolve().parents[2] / "schema" / filename,
        Path(sys.prefix) / "schema" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("compaction continuity schema is not installed")


class CompactionContinuityError(ValueError):
    """Raised for an invalid candidate window or local annotation."""


class CompactionAnnotationNotExportable(CompactionContinuityError):
    """Raised when a continuity annotation is offered to an export path."""


@dataclass(frozen=True)
class CompactionWindow:
    """A bounded, content-free review pointer around one compaction event.

    Tier 1 working artifact. It holds the local ``session_id`` and
    ``compaction_event_id`` so a reviewer can find the events on their own
    machine; neither is copied into the annotation that outlives the review.
    """

    window_id: str
    session_id: str
    compaction_event_id: str
    compaction_sequence_index: int
    pre_event_ids: tuple[str, ...]
    post_event_ids: tuple[str, ...]
    first_substantive_post_event_id: str | None
    annotation_eligible: bool
    ineligible_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "session_id": self.session_id,
            "compaction_event_id": self.compaction_event_id,
            "compaction_sequence_index": self.compaction_sequence_index,
            "pre_event_ids": list(self.pre_event_ids),
            "post_event_ids": list(self.post_event_ids),
            "first_substantive_post_event_id": self.first_substantive_post_event_id,
            "annotation_eligible": self.annotation_eligible,
            "ineligible_reason": self.ineligible_reason,
        }


def _require_event_shape(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        raise CompactionContinuityError("a continuity window requires at least one canonical event")
    required = {"event_id", "event_type", "sequence_index", "session_id"}
    sessions: set[str] = set()
    event_ids: set[str] = set()
    sequence_indexes: set[int] = set()
    for event in events:
        if not required <= event.keys():
            missing = sorted(required - event.keys())
            raise CompactionContinuityError(f"canonical event is missing required window fields: {missing}")
        if not isinstance(event["event_id"], str) or not event["event_id"]:
            raise CompactionContinuityError("event_id must be a non-empty string")
        if not isinstance(event["sequence_index"], int) or event["sequence_index"] < 0:
            raise CompactionContinuityError("sequence_index must be a non-negative integer")
        if not isinstance(event["session_id"], str) or not event["session_id"]:
            raise CompactionContinuityError("session_id must be a non-empty string")
        sessions.add(event["session_id"])
        event_ids.add(event["event_id"])
        sequence_indexes.add(event["sequence_index"])
    if len(sessions) != 1:
        raise CompactionContinuityError("a continuity window input must contain exactly one session")
    if len(event_ids) != len(events):
        raise CompactionContinuityError("event_id values must be unique within a continuity window input")
    if len(sequence_indexes) != len(events):
        raise CompactionContinuityError("sequence_index values must be unique within a continuity window input")
    return sorted(events, key=lambda event: event["sequence_index"])


def window_handle(session_id: str, sequence_index: int, *, window_key: bytes) -> str:
    """Return a keyed, opaque handle for one boundary in one session.

    The handle is deterministic for a given local key, so a reviewer can match
    an annotation back to its window, and it commits to both inputs, so an
    annotation cannot be moved to a different boundary. Without the key it
    reveals neither the session nor the position: it is a digest, not an
    encoding of the identifiers.
    """
    if not isinstance(window_key, bytes) or len(window_key) < MINIMUM_WINDOW_KEY_BYTES:
        raise CompactionContinuityError(
            f"window_key must be at least {MINIMUM_WINDOW_KEY_BYTES} bytes of local key material"
        )
    message = f"{len(session_id)}:{session_id}:{sequence_index}".encode("utf-8")
    return "ccw-" + hmac.new(window_key, message, sha256).hexdigest()[:32]


def extract_compaction_windows(
    events: list[dict[str, Any]],
    *,
    window_key: bytes,
    pre_window_events: int = 8,
    post_window_events: int = 12,
) -> list[CompactionWindow]:
    """Return bounded candidates without interpreting intent or content.

    A window is annotation-eligible only when the boundary is an observed
    boundary marker, a prior user-message anchor exists, substantive agent
    activity follows, and no new user message appears before that first
    activity. The last condition avoids calling a fresh instruction a
    continuity failure.

    Both edges of the window stop at the neighbouring compaction. Reaching back
    past the previous boundary would let an instruction that had itself already
    been compacted away serve as the anchor for this one.

    Ineligible windows are returned with a reason; they are never discarded and
    never silently upgraded.
    """
    if pre_window_events < 1 or post_window_events < 1:
        raise CompactionContinuityError("pre_window_events and post_window_events must both be positive")
    ordered = _require_event_shape(events)
    session_id = ordered[0]["session_id"]
    boundary_positions = [
        position for position, event in enumerate(ordered) if event["event_type"] == "compaction"
    ]
    windows: list[CompactionWindow] = []
    for order, index in enumerate(boundary_positions):
        boundary = ordered[index]
        previous_boundary_index = boundary_positions[order - 1] if order > 0 else -1
        next_boundary_index = (
            boundary_positions[order + 1] if order + 1 < len(boundary_positions) else len(ordered)
        )
        pre_floor = max(previous_boundary_index + 1, index - pre_window_events)
        pre = ordered[pre_floor:index]
        post = ordered[index + 1 : min(next_boundary_index, index + 1 + post_window_events)]

        first_substantive_index = next(
            (position for position, event in enumerate(post) if event["event_type"] in SUBSTANTIVE_EVENT_TYPES),
            None,
        )
        first_substantive_id = (
            post[first_substantive_index]["event_id"] if first_substantive_index is not None else None
        )
        has_prior_user_anchor = any(event["event_type"] == "user_message" for event in pre)

        if boundary.get("event_origin") != "observed":
            eligible, reason = False, "compaction_boundary_not_observed"
        elif (boundary.get("compaction") or {}).get("compaction_source") not in ACCEPTED_COMPACTION_SOURCES:
            eligible, reason = False, "compaction_source_not_accepted"
        elif first_substantive_index is None:
            eligible, reason = False, "no_post_boundary_substantive_activity"
        elif not has_prior_user_anchor:
            eligible, reason = False, "no_prior_user_message_anchor"
        elif any(event["event_type"] == "user_message" for event in post[:first_substantive_index]):
            eligible, reason = False, "new_user_message_before_first_substantive_activity"
        else:
            eligible, reason = True, None

        windows.append(
            CompactionWindow(
                window_id=window_handle(session_id, boundary["sequence_index"], window_key=window_key),
                session_id=session_id,
                compaction_event_id=boundary["event_id"],
                compaction_sequence_index=boundary["sequence_index"],
                pre_event_ids=tuple(event["event_id"] for event in pre),
                post_event_ids=tuple(event["event_id"] for event in post),
                first_substantive_post_event_id=first_substantive_id,
                annotation_eligible=eligible,
                ineligible_reason=reason,
            )
        )
    return windows


def _annotation_schema() -> dict[str, Any]:
    return json.loads(_schema_path().read_text(encoding="utf-8"))


def _check_rationale_codes(outcome: str, codes: set[str]) -> None:
    rules = OUTCOME_RATIONALE_RULES[outcome]
    missing = sorted(rules["required_all"] - codes)
    if missing:
        raise CompactionContinuityError(f"{outcome} requires rationale codes: {missing}")
    if rules["required_any"] and not (rules["required_any"] & codes):
        raise CompactionContinuityError(
            f"{outcome} requires at least one rationale code from: {sorted(rules['required_any'])}"
        )
    contradicting = sorted(rules["forbidden"] & codes)
    if contradicting:
        raise CompactionContinuityError(
            f"{outcome} is contradicted by rationale codes: {contradicting}"
        )


def validate_annotation(annotation: dict[str, Any], window: CompactionWindow) -> None:
    """Validate a content-free local human annotation against its exact window.

    The annotation names no session and no event. It is bound to its window by
    the keyed handle alone, which is why the handle commits to the session and
    the boundary position together.
    """
    try:
        jsonschema.validate(annotation, _annotation_schema())
    except jsonschema.ValidationError as exc:
        raise CompactionContinuityError(f"annotation schema validation failed: {exc.message}") from exc
    if annotation["annotation_version"] != ANNOTATION_VERSION:
        raise CompactionContinuityError(f"unsupported annotation_version: {annotation['annotation_version']!r}")
    if annotation["outcome"] not in ANNOTATION_OUTCOMES:
        raise CompactionContinuityError(f"unsupported annotation outcome: {annotation['outcome']!r}")
    if annotation["window_id"] != window.window_id:
        raise CompactionContinuityError("annotation window_id does not match its candidate window")
    if annotation["outcome"] != "unclear" and not window.annotation_eligible:
        raise CompactionContinuityError("only an unclear annotation may be recorded for an ineligible window")
    reviewed = annotation["reviewed"]
    if annotation["outcome"] != "unclear" and not all(reviewed.values()):
        raise CompactionContinuityError("a determinate annotation requires all local review checks")
    _check_rationale_codes(annotation["outcome"], set(annotation["rationale_codes"]))


def assert_annotation_not_exportable(candidate: Any) -> None:
    """Refuse a continuity annotation anywhere in a structure bound for export.

    A continuity annotation is Tier 1 terminal. It summarises a human's reading
    of protected transcript context, and the fact that a reviewer concluded an
    agent resumed a superseded task is itself a claim about a private session.
    No screening step makes it publishable, so the artifact has no export form
    at all — this refuses rather than redacts.
    """
    if isinstance(candidate, dict):
        if {"window_id", "outcome", "rationale_codes"} <= candidate.keys():
            raise CompactionAnnotationNotExportable(
                "compaction continuity annotations are Tier 1 terminal and are never exportable"
            )
        for value in candidate.values():
            assert_annotation_not_exportable(value)
    elif isinstance(candidate, (list, tuple)):
        for item in candidate:
            assert_annotation_not_exportable(item)
