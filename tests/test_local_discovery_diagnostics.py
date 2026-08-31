"""Local discovery diagnostics: per-diagnostic synthetic tests.

Fixture-only -- every event here is hand-built, matching `agent_event.schema.json`'s
real shape (the same discipline `fixtures/canonical/valid/` already uses), never a
real session. Each of the six diagnostics gets its own test proving: the per-session
value against a hand-computed expectation, an exclusion case where one applies, and
the diagnostic's own `_aggregate_*` function against an *independently* hand-computed
total -- never a value derived by calling the same summation twice.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent_policy_lab.local_discovery_diagnostics import (
    DIAGNOSTICS_SPEC,
    SessionDiagnosticInput,
    compute_assistant_token_usage,
    compute_compaction_count,
    compute_elapsed_event_span,
    compute_paired_tool_outcome,
    compute_reconciliation_availability,
    compute_tool_invocation_distribution,
)


def _event(event_type: str, sequence_index: int, timestamp: str | None, field_status: dict, payload: dict) -> dict:
    """A schema-shaped canonical event with the fields these calculators actually
    read populated meaningfully, and the rest filled with fixture-appropriate
    boilerplate -- not a real session, and not validated against the schema here
    (that's `fixtures/canonical/`'s job); this is a unit-test input."""
    return {
        "schema_version": "0.1.1",
        "event_id": f"evt-{sequence_index}",
        "sequence_index": sequence_index,
        "run_id": "fixture-run",
        "source_client": "claude-code",
        "source_client_version": "2.1.220",
        "session_id": "fixture-session",
        "repo_ref": "sha256:fixture",
        "turn_index": sequence_index,
        "event_type": event_type,
        "event_origin": "observed",
        "field_status": field_status,
        "timestamp": timestamp,
        event_type: payload,
    }


def _ctx(events: list[dict], reconciliation: dict | None = None) -> SessionDiagnosticInput:
    return SessionDiagnosticInput(events=events, reconciliation=reconciliation or {})


# ---------------------------------------------------------------------------
# reconciliation_availability
# ---------------------------------------------------------------------------

_ORDINARY_RECONCILIATION = {
    cls: {"emitted": 0, "raw": 0, "ok": True}
    for cls in ("tool_call", "tool_result", "thinking", "user_message", "assistant_message", "compaction", "turns")
}


def _reconciliation(**overrides) -> dict:
    r = {k: dict(v) for k, v in _ORDINARY_RECONCILIATION.items()}
    r["_preserved"] = {
        "prompt_id_only_envelope_raw": 0, "prompt_id_only_envelope_preserved": 0,
        "user_record_without_prompt_source_raw": 0, "user_record_without_prompt_source_preserved": 0,
        "meta_injection_raw": 0, "meta_injection_preserved": 0,
        "ok": True,
    }
    for cls, values in overrides.items():
        r[cls].update(values)
    return r


def test_compute_reconciliation_availability_converts_to_normalized_shape():
    recon = _reconciliation(tool_call={"emitted": 5, "raw": 5}, tool_result={"emitted": 4, "raw": 4})
    leaf = compute_reconciliation_availability(_ctx([], recon))
    assert leaf["outcome"] == "computed"
    assert leaf["value"]["tool_call"] == {"raw_count": 5, "canonical_count": 5, "delta": 0}
    assert leaf["value"]["tool_result"] == {"raw_count": 4, "canonical_count": 4, "delta": 0}
    assert "ok" not in leaf["value"]  # never carried as a measured value
    assert leaf["provenance"]["source"] == "adapter_reconciliation"
    assert leaf["provenance"]["rule_version"] == "1.0.0"


def test_compute_reconciliation_availability_preserved_categories():
    recon = _reconciliation()
    recon["_preserved"]["meta_injection_raw"] = 2
    recon["_preserved"]["meta_injection_preserved"] = 2
    leaf = compute_reconciliation_availability(_ctx([], recon))
    assert leaf["value"]["preserved"]["meta_injection"] == {
        "raw_count": 2, "preserved_count": 2, "delta": 0,
    }


def test_aggregate_reconciliation_availability_sums_and_recomputes_delta():
    from agent_policy_lab.local_discovery_diagnostics import _aggregate_reconciliation_availability

    leaf_a = compute_reconciliation_availability(_ctx([], _reconciliation(tool_call={"emitted": 5, "raw": 5})))
    leaf_b = compute_reconciliation_availability(_ctx([], _reconciliation(tool_call={"emitted": 3, "raw": 3})))
    aggregate = _aggregate_reconciliation_availability({"S1": leaf_a, "S2": leaf_b})
    # independently hand-computed: 5 + 3 = 8
    assert aggregate["value"]["tool_call"] == {"raw_count": 8, "canonical_count": 8, "delta": 0}
    assert aggregate["outcome"] == "computed"
    assert aggregate["provenance"]["source"] == "adapter_reconciliation"


# ---------------------------------------------------------------------------
# assistant_token_usage
# ---------------------------------------------------------------------------


def test_compute_assistant_token_usage_sums_observed_events_and_excludes_unavailable():
    events = [
        _event("assistant_message", 1, "2026-01-01T00:00:00Z",
               {"input_tokens": "observed", "output_tokens": "observed"},
               {"input_tokens": 100, "output_tokens": 20}),
        _event("assistant_message", 2, "2026-01-01T00:01:00Z",
               {"input_tokens": "observed", "output_tokens": "observed"},
               {"input_tokens": 50, "output_tokens": 10}),
        _event("assistant_message", 3, "2026-01-01T00:02:00Z",
               {"input_tokens": "unavailable", "output_tokens": "unavailable"},
               {"input_tokens": None, "output_tokens": None}),
    ]
    leaf = compute_assistant_token_usage(_ctx(events))
    # hand-computed: 100+50=150, 20+10=30; the third event is excluded
    assert leaf["outcome"] == "computed"
    assert leaf["value"] == {"input_tokens": 150, "output_tokens": 30}
    assert leaf["provenance"]["excluded_events"] == 1


def test_compute_assistant_token_usage_not_reportable_when_all_excluded():
    events = [
        _event("assistant_message", 1, "2026-01-01T00:00:00Z",
               {"input_tokens": "unavailable", "output_tokens": "unavailable"},
               {"input_tokens": None, "output_tokens": None}),
    ]
    leaf = compute_assistant_token_usage(_ctx(events))
    assert leaf["outcome"] == "not_reportable"
    assert leaf["reason"]


def test_aggregate_assistant_token_usage_sums_only_computed_sessions():
    from agent_policy_lab.local_discovery_diagnostics import _aggregate_assistant_token_usage

    computed_leaf = {"outcome": "computed", "value": {"input_tokens": 150, "output_tokens": 30}}
    other_leaf = {"outcome": "computed", "value": {"input_tokens": 10, "output_tokens": 5}}
    not_reportable_leaf = {"outcome": "not_reportable", "reason": "x"}
    aggregate = _aggregate_assistant_token_usage(
        {"S1": computed_leaf, "S2": other_leaf, "S3": not_reportable_leaf}
    )
    # independently hand-computed: 150+10=160, 30+5=35; S3 contributes nothing
    assert aggregate["value"] == {"total_input_tokens": 160, "total_output_tokens": 35}


# ---------------------------------------------------------------------------
# elapsed_event_span
# ---------------------------------------------------------------------------


def test_compute_elapsed_event_span_sums_first_to_last_by_sequence_index():
    events = [
        _event("user_message", 2, "2026-01-01T00:05:00Z", {}, {}),
        _event("user_message", 1, "2026-01-01T00:00:00Z", {}, {}),
        _event("assistant_message", 3, "2026-01-01T00:07:30Z", {}, {}),
    ]
    leaf = compute_elapsed_event_span(_ctx(events))
    # sorted by sequence_index: 00:00:00 -> 00:07:30 = 450 seconds
    assert leaf["outcome"] == "computed"
    assert leaf["value"] == {"elapsed_seconds": 450}
    # timestamp is a bare envelope field -- evidenced by presence/absence
    # (envelope_timestamp_presence), never a borrowed payload field_status
    for inp in leaf["provenance"]["inputs"]:
        assert inp["basis"] == "envelope_timestamp_presence"
        assert inp["status"] == "observed"
        assert "field_status" not in inp


def test_compute_elapsed_event_span_not_reportable_with_fewer_than_two_events():
    events = [_event("user_message", 1, "2026-01-01T00:00:00Z", {}, {})]
    leaf = compute_elapsed_event_span(_ctx(events))
    assert leaf["outcome"] == "not_reportable"


def test_compute_elapsed_event_span_not_reportable_when_timestamp_unavailable():
    events = [
        _event("user_message", 1, None, {}, {}),
        _event("assistant_message", 2, "2026-01-01T00:01:00Z", {}, {}),
    ]
    leaf = compute_elapsed_event_span(_ctx(events))
    assert leaf["outcome"] == "not_reportable"


def test_aggregate_elapsed_event_span_is_sum_never_mean_or_single_session():
    from agent_policy_lab.local_discovery_diagnostics import _aggregate_elapsed_event_span

    leaf_a = {"outcome": "computed", "value": {"elapsed_seconds": 100}}
    leaf_b = {"outcome": "computed", "value": {"elapsed_seconds": 300}}
    aggregate = _aggregate_elapsed_event_span({"S1": leaf_a, "S2": leaf_b})
    # independently hand-computed sum: 100 + 300 = 400 (mean would be 200; a single
    # session's own value would be 100 or 300 -- neither is the right answer)
    assert aggregate["value"] == {"total_elapsed_seconds": 400}


# ---------------------------------------------------------------------------
# compaction_count
# ---------------------------------------------------------------------------


def test_compute_compaction_count():
    events = [
        _event("compaction", 1, "2026-01-01T00:00:00Z", {}, {}),
        _event("compaction", 2, "2026-01-01T00:01:00Z", {}, {}),
        _event("user_message", 3, "2026-01-01T00:02:00Z", {}, {}),
    ]
    leaf = compute_compaction_count(_ctx(events))
    assert leaf["outcome"] == "computed"
    assert leaf["value"] == {"count": 2}
    # event_type is a schema-required envelope classification, always
    # populated -- it carries no field_status/status key at all, never claiming a
    # variability that doesn't exist
    assert leaf["provenance"]["inputs"] == [
        {"field": "event_type", "basis": "required_envelope_classification"}
    ]


def test_compute_compaction_count_is_zero_not_not_reportable_when_absent():
    events = [_event("user_message", 1, "2026-01-01T00:00:00Z", {}, {})]
    leaf = compute_compaction_count(_ctx(events))
    assert leaf["outcome"] == "computed"
    assert leaf["value"] == {"count": 0}


def test_aggregate_compaction_count_sums_sessions():
    from agent_policy_lab.local_discovery_diagnostics import _aggregate_compaction_count

    aggregate = _aggregate_compaction_count(
        {"S1": {"value": {"count": 2}}, "S2": {"value": {"count": 5}}}
    )
    # independently hand-computed: 2 + 5 = 7
    assert aggregate["value"] == {"total_count": 7}


# ---------------------------------------------------------------------------
# tool_invocation_distribution
# ---------------------------------------------------------------------------


def test_compute_tool_invocation_distribution_counts_and_excludes_unavailable_names():
    events = [
        _event("tool_call", 1, "t", {"tool_name": "observed"}, {"tool_name": "Read"}),
        _event("tool_call", 2, "t", {"tool_name": "observed"}, {"tool_name": "Read"}),
        _event("tool_call", 3, "t", {"tool_name": "observed"}, {"tool_name": "Bash"}),
        _event("tool_call", 4, "t", {"tool_name": "unavailable"}, {"tool_name": None}),
    ]
    leaf = compute_tool_invocation_distribution(_ctx(events))
    assert leaf["outcome"] == "computed"
    assert leaf["value"] == {"Read": 2, "Bash": 1}
    assert leaf["provenance"]["excluded_unknown_tool_name"] == 1


def test_aggregate_tool_invocation_distribution_merges_dynamic_keys():
    from agent_policy_lab.local_discovery_diagnostics import _aggregate_tool_invocation_distribution

    leaf_a = {"value": {"Read": 2, "Bash": 1}, "provenance": {"excluded_unknown_tool_name": 1}}
    leaf_b = {"value": {"Read": 1, "Write": 3}, "provenance": {"excluded_unknown_tool_name": 0}}
    aggregate = _aggregate_tool_invocation_distribution({"S1": leaf_a, "S2": leaf_b})
    # independently hand-computed: Read=2+1=3, Bash=1+0=1, Write=0+3=3
    assert aggregate["value"] == {"Read": 3, "Bash": 1, "Write": 3}
    assert aggregate["provenance"]["excluded_unknown_tool_name_total"] == 1


# ---------------------------------------------------------------------------
# paired_tool_outcome_observed_success
# ---------------------------------------------------------------------------


def test_compute_paired_tool_outcome_joins_and_excludes():
    events = [
        # c1: matched, success
        _event("tool_call", 1, "t", {"tool_correlation_id": "inferred"}, {"tool_correlation_id": "c1"}),
        _event("tool_result", 2, "t", {"tool_correlation_id": "inferred", "success": "observed"},
               {"tool_correlation_id": "c1", "success": True}),
        # c2: matched, failure
        _event("tool_call", 3, "t", {"tool_correlation_id": "inferred"}, {"tool_correlation_id": "c2"}),
        _event("tool_result", 4, "t", {"tool_correlation_id": "inferred", "success": "observed"},
               {"tool_correlation_id": "c2", "success": False}),
        # c3: no matching result at all -> excluded
        _event("tool_call", 5, "t", {"tool_correlation_id": "inferred"}, {"tool_correlation_id": "c3"}),
        # c4: matched, but success is unavailable -> excluded
        _event("tool_call", 6, "t", {"tool_correlation_id": "inferred"}, {"tool_correlation_id": "c4"}),
        _event("tool_result", 7, "t", {"tool_correlation_id": "inferred", "success": "unavailable"},
               {"tool_correlation_id": "c4", "success": None}),
    ]
    leaf = compute_paired_tool_outcome(_ctx(events))
    assert leaf["outcome"] == "computed"
    assert leaf["value"] == {"observed_success": 1, "observed_failure": 1}
    assert leaf["provenance"]["excluded_unknown_success"] == 2


def test_aggregate_paired_tool_outcome_sums_across_sessions():
    from agent_policy_lab.local_discovery_diagnostics import _aggregate_paired_tool_outcome

    leaf_a = {"value": {"observed_success": 1, "observed_failure": 1}, "provenance": {"excluded_unknown_success": 2}}
    leaf_b = {"value": {"observed_success": 3, "observed_failure": 0}, "provenance": {"excluded_unknown_success": 1}}
    aggregate = _aggregate_paired_tool_outcome({"S1": leaf_a, "S2": leaf_b})
    # independently hand-computed: success 1+3=4, failure 1+0=1, excluded 2+1=3
    assert aggregate["value"] == {"observed_success": 4, "observed_failure": 1}
    assert aggregate["provenance"]["excluded_unknown_success_total"] == 3


# ---------------------------------------------------------------------------
# DIAGNOSTICS_SPEC: one genuine source of truth, immutable
# ---------------------------------------------------------------------------

_APPROVED_DIAGNOSTIC_NAMES = {
    "reconciliation_availability",
    "assistant_token_usage",
    "elapsed_event_span",
    "compaction_count",
    "tool_invocation_distribution",
    "paired_tool_outcome_observed_success",
}


def test_diagnostics_spec_names_match_the_approved_six_exactly():
    assert {d.name for d in DIAGNOSTICS_SPEC} == _APPROVED_DIAGNOSTIC_NAMES


def test_diagnostics_spec_every_entry_is_dispatchable():
    for d in DIAGNOSTICS_SPEC:
        assert callable(d.calculate), f"{d.name} has no calculate callable"
        assert callable(d.aggregate), f"{d.name} has no aggregate callable"


def test_diagnostics_spec_is_immutable():
    assert isinstance(DIAGNOSTICS_SPEC, tuple)
    for d in DIAGNOSTICS_SPEC:
        assert isinstance(d.signal_vocabulary, tuple)
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.name = "mutated"  # type: ignore[misc]
