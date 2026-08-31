from __future__ import annotations

import copy

import pytest

from agent_policy_lab.compaction_continuity import (
    ANNOTATION_OUTCOMES,
    CompactionAnnotationNotExportable,
    CompactionContinuityError,
    OUTCOME_RATIONALE_RULES,
    assert_annotation_not_exportable,
    extract_compaction_windows,
    validate_annotation,
    window_handle,
)

KEY = b"local-window-key-fixture-material"
OTHER_KEY = b"a-different-local-window-key-here"


def event(event_id: str, sequence_index: int, event_type: str, session_id: str = "fixture-session") -> dict:
    return {
        "event_id": event_id,
        "sequence_index": sequence_index,
        "event_type": event_type,
        "session_id": session_id,
    }


def boundary(
    event_id: str,
    sequence_index: int,
    session_id: str = "fixture-session",
    *,
    origin: str = "observed",
    source: str | None = "boundary_marker",
) -> dict:
    record = event(event_id, sequence_index, "compaction", session_id)
    record["event_origin"] = origin
    record["compaction"] = {"compaction_source": source}
    return record


def extract(events, **kwargs):
    return extract_compaction_windows(events, window_key=KEY, **kwargs)


def eligible_window():
    return extract(
        [
            event("u1", 0, "user_message"),
            event("a1", 1, "assistant_message"),
            boundary("c1", 2),
            event("t1", 3, "tool_call"),
        ]
    )[0]


def complete_annotation(window, outcome: str = "continued_current_task") -> dict:
    codes = {
        "continued_current_task": ["latest_task_addressed"],
        "resumed_superseded_task": ["latest_task_not_addressed", "superseded_task_addressed"],
        "legitimate_revisit": ["latest_task_addressed", "user_authorised_revisit"],
        "unclear": ["insufficient_context"],
    }[outcome]
    return {
        "annotation_version": "1.0.0",
        "window_id": window.window_id,
        "review_mode": "local_human_review",
        "reviewer_role": "operator",
        "outcome": outcome,
        "rationale_codes": codes,
        "reviewed": {
            "pre_boundary_context": True,
            "latest_active_instruction": True,
            "post_boundary_activity": True,
            "earlier_superseded_instruction": True,
        },
    }


# --- window extraction ---------------------------------------------------


def test_extracts_a_content_free_eligible_window():
    window = eligible_window()
    assert window.annotation_eligible is True
    assert window.pre_event_ids == ("u1", "a1")
    assert window.post_event_ids == ("t1",)
    assert window.first_substantive_post_event_id == "t1"


def test_keeps_non_eligible_windows_with_an_explicit_reason():
    windows = extract([event("u1", 0, "user_message"), boundary("c1", 1)])
    assert windows[0].annotation_eligible is False
    assert windows[0].ineligible_reason == "no_post_boundary_substantive_activity"


def test_new_user_instruction_before_activity_is_not_a_continuity_candidate():
    windows = extract(
        [
            event("u1", 0, "user_message"),
            boundary("c1", 1),
            event("u2", 2, "user_message"),
            event("t1", 3, "tool_call"),
        ]
    )
    assert windows[0].annotation_eligible is False
    assert windows[0].ineligible_reason == "new_user_message_before_first_substantive_activity"


def test_tool_result_alone_is_not_substantive_post_boundary_activity():
    """A post-boundary tool_result usually completes a call made before it."""
    windows = extract(
        [
            event("u1", 0, "user_message"),
            event("tc1", 1, "tool_call"),
            boundary("c1", 2),
            event("tr1", 3, "tool_result"),
        ]
    )
    assert windows[0].annotation_eligible is False
    assert windows[0].ineligible_reason == "no_post_boundary_substantive_activity"
    assert windows[0].first_substantive_post_event_id is None


def test_tool_result_does_not_mask_the_later_first_substantive_event():
    windows = extract(
        [
            event("u1", 0, "user_message"),
            boundary("c1", 1),
            event("tr1", 2, "tool_result"),
            event("a1", 3, "assistant_message"),
        ]
    )
    assert windows[0].first_substantive_post_event_id == "a1"
    assert windows[0].annotation_eligible is True


# --- multiple boundaries -------------------------------------------------


def test_multiple_boundaries_do_not_cross_window_boundaries():
    windows = extract(
        [
            event("u1", 0, "user_message"),
            boundary("c1", 1),
            event("t1", 2, "tool_call"),
            boundary("c2", 3),
            event("t2", 4, "tool_call"),
        ]
    )
    assert [window.post_event_ids for window in windows] == [("t1",), ("t2",)]


def test_pre_window_is_clamped_after_the_preceding_boundary():
    """An instruction already compacted away cannot anchor the next boundary."""
    windows = extract(
        [
            event("u1", 0, "user_message"),
            boundary("c1", 1),
            event("a1", 2, "assistant_message"),
            boundary("c2", 3),
            event("a2", 4, "assistant_message"),
        ]
    )
    assert windows[1].pre_event_ids == ("a1",)
    assert "c1" not in windows[1].pre_event_ids
    assert windows[1].annotation_eligible is False
    assert windows[1].ineligible_reason == "no_prior_user_message_anchor"


def test_each_boundary_gets_its_own_distinct_handle():
    windows = extract(
        [
            event("u1", 0, "user_message"),
            boundary("c1", 1),
            event("a1", 2, "assistant_message"),
            boundary("c2", 3),
            event("a2", 4, "assistant_message"),
        ]
    )
    assert windows[0].window_id != windows[1].window_id


# --- boundary provenance -------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "source", "reason"),
    [
        ("synthesized", "boundary_marker", "compaction_boundary_not_observed"),
        ("observed", "continuation_summary", "compaction_source_not_accepted"),
        ("observed", "unavailable", "compaction_source_not_accepted"),
        ("observed", None, "compaction_source_not_accepted"),
    ],
)
def test_only_an_observed_boundary_marker_is_an_eligible_candidate(origin, source, reason):
    windows = extract(
        [
            event("u1", 0, "user_message"),
            boundary("c1", 1, origin=origin, source=source),
            event("t1", 2, "tool_call"),
        ]
    )
    assert windows[0].annotation_eligible is False
    assert windows[0].ineligible_reason == reason
    # Retained, not dropped: the window is still visible with its bounds.
    assert windows[0].post_event_ids == ("t1",)


def test_a_compaction_record_without_provenance_fields_fails_closed():
    bare = event("c1", 1, "compaction")
    windows = extract([event("u1", 0, "user_message"), bare, event("t1", 2, "tool_call")])
    assert windows[0].annotation_eligible is False
    assert windows[0].ineligible_reason == "compaction_boundary_not_observed"


# --- window handle -------------------------------------------------------


def test_window_handle_is_opaque_and_keyed():
    handle = window_handle("fixture-session", 2, window_key=KEY)
    digest = handle.removeprefix("ccw-")
    assert handle.startswith("ccw-")
    assert "fixture-session" not in handle
    assert len(digest) == 32 and all(character in "0123456789abcdef" for character in digest)
    assert handle != window_handle("fixture-session", 2, window_key=OTHER_KEY)
    assert handle == window_handle("fixture-session", 2, window_key=KEY)


def test_window_handle_commits_to_both_session_and_position():
    assert window_handle("a", 12, window_key=KEY) != window_handle("a", 1, window_key=KEY)
    assert window_handle("a1", 2, window_key=KEY) != window_handle("a", 12, window_key=KEY)
    assert window_handle("session-a", 1, window_key=KEY) != window_handle("session-b", 1, window_key=KEY)


@pytest.mark.parametrize("bad_key", [b"", b"too-short", "not-bytes-at-all-but-long-enough"])
def test_window_handle_refuses_weak_or_wrong_key_material(bad_key):
    with pytest.raises(CompactionContinuityError, match="window_key"):
        window_handle("fixture-session", 0, window_key=bad_key)


def test_extraction_requires_a_window_key():
    with pytest.raises(TypeError):
        extract_compaction_windows([event("u1", 0, "user_message")])


# --- input validation ----------------------------------------------------


@pytest.mark.parametrize(
    "events",
    [
        [event("same", 0, "user_message"), boundary("same", 1)],
        [event("u1", 0, "user_message"), boundary("c1", 0)],
        [event("u1", 0, "user_message", "one"), boundary("c1", 1, "two")],
    ],
)
def test_refuses_ambiguous_window_input(events):
    with pytest.raises(CompactionContinuityError):
        extract(events)


# --- annotation ----------------------------------------------------------


def test_validates_a_complete_local_annotation():
    window = eligible_window()
    validate_annotation(complete_annotation(window, "resumed_superseded_task"), window)


def test_annotation_carries_no_session_or_event_identifier():
    window = eligible_window()
    annotation = complete_annotation(window)
    assert "session_id" not in annotation
    assert "compaction_event_id" not in annotation
    serialised = repr(annotation)
    assert window.session_id not in serialised
    assert window.compaction_event_id not in serialised


def test_refuses_annotation_for_the_wrong_window():
    window = eligible_window()
    annotation = complete_annotation(window)
    annotation["window_id"] = window_handle("fixture-session", 99, window_key=KEY)
    with pytest.raises(CompactionContinuityError, match="window_id"):
        validate_annotation(annotation, window)


def test_refuses_an_annotation_handle_made_with_a_different_key():
    window = eligible_window()
    annotation = complete_annotation(window)
    annotation["window_id"] = window_handle(window.session_id, 2, window_key=OTHER_KEY)
    with pytest.raises(CompactionContinuityError, match="window_id"):
        validate_annotation(annotation, window)


def test_refuses_determinate_label_without_complete_local_review():
    window = eligible_window()
    annotation = complete_annotation(window)
    annotation["reviewed"]["latest_active_instruction"] = False
    with pytest.raises(CompactionContinuityError, match="determinate annotation"):
        validate_annotation(annotation, window)


def test_only_unclear_may_be_recorded_for_an_ineligible_window():
    window = extract([event("u1", 0, "user_message"), boundary("c1", 1)])[0]
    determinate = complete_annotation(window)
    with pytest.raises(CompactionContinuityError, match="only an unclear"):
        validate_annotation(determinate, window)
    unclear = complete_annotation(window, "unclear")
    unclear["reviewed"]["post_boundary_activity"] = False
    validate_annotation(unclear, window)


def test_schema_refusal_leaves_no_free_text_escape_hatch():
    window = eligible_window()
    annotation = copy.deepcopy(complete_annotation(window))
    annotation["rationale"] = "quoted transcript text must not be stored here"
    with pytest.raises(CompactionContinuityError, match="annotation schema"):
        validate_annotation(annotation, window)


# --- outcome/rationale consistency ---------------------------------------


def test_every_outcome_has_a_rationale_rule():
    assert set(OUTCOME_RATIONALE_RULES) == set(ANNOTATION_OUTCOMES)


def test_refuses_resumed_label_without_both_task_continuity_codes():
    window = eligible_window()
    annotation = complete_annotation(window, "resumed_superseded_task")
    annotation["rationale_codes"] = ["superseded_task_addressed"]
    with pytest.raises(CompactionContinuityError, match="requires rationale codes"):
        validate_annotation(annotation, window)


@pytest.mark.parametrize("revisit_code", ["user_authorised_revisit", "dependency_requires_revisit"])
def test_resumed_label_cannot_also_claim_a_legitimate_revisit(revisit_code):
    """The exact ambiguity the outcome must be distinguishable from."""
    window = eligible_window()
    annotation = complete_annotation(window, "resumed_superseded_task")
    annotation["rationale_codes"] = [*annotation["rationale_codes"], revisit_code]
    with pytest.raises(CompactionContinuityError, match="contradicted by"):
        validate_annotation(annotation, window)


def test_legitimate_revisit_requires_a_revisit_justification():
    window = eligible_window()
    annotation = complete_annotation(window, "legitimate_revisit")
    annotation["rationale_codes"] = ["latest_task_addressed"]
    with pytest.raises(CompactionContinuityError, match="at least one rationale code"):
        validate_annotation(annotation, window)


@pytest.mark.parametrize(
    ("outcome", "codes"),
    [
        ("continued_current_task", ["latest_task_addressed", "latest_task_not_addressed"]),
        ("continued_current_task", ["latest_task_addressed", "superseded_task_addressed"]),
        ("legitimate_revisit", ["user_authorised_revisit", "latest_task_not_addressed"]),
        ("unclear", ["insufficient_context", "latest_task_addressed"]),
        ("unclear", ["insufficient_context", "superseded_task_addressed"]),
    ],
)
def test_refuses_self_contradicting_rationale_combinations(outcome, codes):
    window = eligible_window()
    annotation = complete_annotation(window, outcome)
    annotation["rationale_codes"] = codes
    with pytest.raises(CompactionContinuityError, match="contradicted by"):
        validate_annotation(annotation, window)


def test_unclear_requires_an_uncertainty_code():
    window = eligible_window()
    annotation = complete_annotation(window, "unclear")
    annotation["rationale_codes"] = ["ambiguous_task_state"]
    validate_annotation(annotation, window)


# --- Tier 1 terminal ------------------------------------------------------


def test_annotations_are_never_exportable():
    window = eligible_window()
    annotation = complete_annotation(window, "resumed_superseded_task")
    with pytest.raises(CompactionAnnotationNotExportable, match="Tier 1 terminal"):
        assert_annotation_not_exportable(annotation)


def test_a_nested_annotation_is_found_at_any_depth():
    window = eligible_window()
    bundle = {"bundle_version": "1.0.0", "notes": [{"attachments": [complete_annotation(window)]}]}
    with pytest.raises(CompactionAnnotationNotExportable):
        assert_annotation_not_exportable(bundle)


def test_an_ordinary_screened_bundle_is_not_mistaken_for_an_annotation():
    assert_annotation_not_exportable(
        {"bundle_version": "1.0.0", "events": [{"event_id": "e1", "outcome": "n/a"}]}
    )


def test_export_validation_refuses_a_bundle_carrying_an_annotation():
    """The structural export gate catches it independently of the helper."""
    from agent_policy_lab.export_validator import ExportRefused, validate_export

    window = eligible_window()
    bundle = {"bundle_version": "1.0.0", "events": [], "notes": [complete_annotation(window)]}
    with pytest.raises(ExportRefused) as excinfo:
        validate_export(bundle, run={}, manifest={}, signoff=None)
    joined = "\n".join(excinfo.value.reasons)
    for key in ("window_id", "rationale_codes", "review_mode", "reviewer_role"):
        assert f"prohibited field: {key!r}" in joined
