"""Tests for the provisional managed environment Claude Code trace-profile adapter.

Synthetic raw fixtures only — no real managed environment trace content, per
the managed-environment trace profile contract and `fixtures/raw/README.md`'s
existing discipline. See
`fixtures/raw/managed_environment_claude/managed_environment_session_shapes.jsonl` for the
shape catalogue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_policy_lab.adapter_managed_environment_claude_code import (
    PRESERVED,
    RAW_TYPE_DISPOSITION,
    SYSTEM_SUBTYPE_DISPOSITION,
    UNSUPPORTED,
    TraceProfileUnavailable,
    UnrecognizedRawType,
    normalize_managed_environment_session,
    reconcile,
)
from agent_policy_lab.snapshot import SessionSnapshot, SnapshotMismatch
from agent_policy_lab.validators import validate_dataset

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "raw" / "managed_environment_claude" / "managed_environment_session_shapes.jsonl"
UNKNOWN_SUBTYPE_FIXTURE = (
    ROOT / "fixtures" / "raw" / "managed_environment_claude" / "managed_environment_session_unknown_system_subtype.jsonl"
)
RUN_ID = "7a2c4e6f-1b3d-4a5c-9e7f-2d4b6c8e0a1f"
SECRET = b"fixture-local-secret-not-a-real-one"

# Raw content markers that must never appear anywhere in the emitted
# canonical events or run record — the privacy-field-dropping check.
# Raw tool-use ids (e.g. "toolu_A") contain letters outside a-f, so they
# cannot coincidentally appear inside a hex uuid/HMAC/digest string — safe
# as plain substrings. Raw record uuids ("c1".."c11") use only hex-valid
# characters, so they're checked as exact quoted JSON string tokens
# (`"c1"`) instead of bare substrings — a bare "c1" would spuriously match
# inside any long hex id, which is not what this check is trying to catch.
FORBIDDEN_SUBSTRINGS = (
    "/CORP/repo",                 # cwd
    "feature/secret-initiative",  # gitBranch
    "proprietary pricing model",  # user message text
    "internal plan: check",       # thinking text
    "internal-build-step",        # bash command text
    "claude-sonnet-5",            # model identity
    "pricing.py\nutils.py",       # tool result content
    "toolu_A", "toolu_ORPHAN", "toolu_C",  # raw tool-use ids
    "BASE64DATA",                 # file-history-snapshot content
    "diff content",               # file-history-delta content
    "internal-deploy-tool",       # attachment addedNames content
    "Q3 roadmap",                 # away_summary free text
    "git push origin",            # local_command raw command text
) + tuple(
    f'"{u}"' for u in
    ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9", "c10", "c11", "c12", "c13", "c14")
)


@pytest.fixture(scope="module")
def event_schema():
    return Draft202012Validator(
        json.loads((ROOT / "schema/v0.1/agent_event.schema.json").read_text())
    )


@pytest.fixture(scope="module")
def run_schema():
    return Draft202012Validator(
        json.loads((ROOT / "schema/v0.1/experiment_run.schema.json").read_text())
    )


@pytest.fixture(scope="module")
def result():
    return normalize_managed_environment_session(
        FIXTURE,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
    )


# --- schema and dataset validation, same gate as the personal adapter -----


def test_every_event_validates(result, event_schema):
    for ev in result.events:
        errors = sorted(event_schema.iter_errors(ev), key=lambda e: list(e.path))
        assert not errors, (
            f"{ev['event_type']} seq={ev['sequence_index']} failed: "
            + "; ".join(e.message for e in errors[:3])
        )


def test_run_record_validates(result, run_schema):
    errors = sorted(run_schema.iter_errors(result.run), key=lambda e: list(e.path))
    assert not errors, "; ".join(e.message for e in errors)


def test_dataset_validators_pass(result):
    report = validate_dataset(result.events, [result.run])
    assert report.ok, "\n".join(report.findings)


# --- not a new client (trace-profile contract) -----------------------------


def test_source_client_is_unchanged_claude_code(result):
    assert all(ev["source_client"] == "claude-code" for ev in result.events)
    assert "managed environment" not in result.run["schema_version"]


# --- run_kind/disposition are hardcoded, never evidence --------------------


def test_run_is_hardcoded_calibration_excluded(result):
    assert result.run["run_kind"] == "calibration"
    assert result.run["disposition"] == "excluded"
    assert result.run["disposition_reason"]


# --- model identity is never read, aggregated, or surfaced ----------------


def test_model_identity_never_surfaced(result):
    assert result.run["model"] is None
    assert result.run["model_source"] is None
    assert result.run["model_evidence_consistent"] is None
    for ev in result.events:
        assert "model" not in (ev.get("assistant_message") or {})


# --- thinking content is never measured ------------------------------------


def test_thinking_length_always_unavailable(result):
    thinking_events = [e for e in result.events if e["event_type"] == "thinking"]
    assert thinking_events, "fixture must exercise at least one thinking block"
    for ev in thinking_events:
        assert ev["thinking"]["length_value"] is None
        assert ev["field_status"]["length_value"] == "unavailable"


# --- tool name / trigger label allowlisting --------------------------------


def test_tool_name_unavailable_by_default(result):
    tool_calls = [e for e in result.events if e["event_type"] == "tool_call"]
    assert tool_calls, "fixture must exercise at least one tool_call"
    for ev in tool_calls:
        assert "tool_name" not in ev["tool_call"]
        assert "tool_name" not in ev["field_status"]


def test_tool_name_surfaced_when_allowlisted():
    allowed = normalize_managed_environment_session(
        FIXTURE,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
        tool_name_allowlist=frozenset({"Read"}),
    )
    tool_calls = [ev for ev in allowed.events if ev["event_type"] == "tool_call"]
    read_call = next(
        ev for ev in tool_calls
        if ev["raw_source_ref"]["line_or_offset"] == 4
    )
    assert read_call["tool_call"]["tool_name"] == "Read"
    assert read_call["field_status"]["tool_name"] == "observed"
    # Bash/Glob (line 6) are not on the allowlist and stay unavailable.
    non_allowed = [ev for ev in tool_calls if ev["raw_source_ref"]["line_or_offset"] == 6]
    assert len(non_allowed) == 2
    for ev in non_allowed:
        assert "tool_name" not in ev["tool_call"]
        assert "tool_name" not in ev["field_status"]


def test_compaction_trigger_unavailable_by_default(result):
    boundary = next(
        ev for ev in result.events
        if ev["event_type"] == "compaction"
        and ev["compaction"]["compaction_source"] == "boundary_marker"
    )
    assert boundary["compaction"]["trigger"] is None
    assert boundary["field_status"]["trigger"] == "unavailable"
    # Numeric compaction metrics are still the approved ceiling fields.
    assert boundary["compaction"]["pre_tokens"] == 90000
    assert boundary["field_status"]["pre_tokens"] == "observed"


def test_compaction_trigger_surfaced_when_allowlisted():
    allowed = normalize_managed_environment_session(
        FIXTURE,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
        trigger_label_allowlist=frozenset({"auto"}),
    )
    boundary = next(
        ev for ev in allowed.events
        if ev["event_type"] == "compaction"
        and ev["compaction"]["compaction_source"] == "boundary_marker"
    )
    assert boundary["compaction"]["trigger"] == "auto"
    assert boundary["field_status"]["trigger"] == "observed"


# --- mode/permission/turn-duration preserved, never a boundary -------------


def test_mode_permission_mode_disposition_is_preserved():
    """Direct assertion against the reviewed policy table itself, not just
    the absence of an emitted event — a record accidentally reclassified
    from `preserved` to `unsupported` (or vice versa) would still pass a
    no-event-emitted check, since neither disposition emits one."""
    assert RAW_TYPE_DISPOSITION["mode"] == PRESERVED
    assert RAW_TYPE_DISPOSITION["permission-mode"] == PRESERVED


def test_turn_duration_subtype_disposition_is_preserved():
    assert SYSTEM_SUBTYPE_DISPOSITION["turn_duration"] == PRESERVED


def test_mode_permission_turn_duration_preserved(result):
    assert result.raw_type_counts["mode"] == 1
    assert result.raw_type_counts["permission-mode"] == 1
    # line 7 is the turn_duration system subtype
    boundary_events = [
        e for e in result.events if e["event_type"] in ("session_start", "session_end")
    ]
    assert len(boundary_events) == 2, (
        "mode/permission-mode/turn_duration records must never themselves "
        "produce a session boundary event — only the two synthesized ones"
    )
    assert boundary_events[0]["event_type"] == "session_start"
    assert boundary_events[0]["event_origin"] == "synthesized"
    assert boundary_events[-1]["event_type"] == "session_end"
    assert boundary_events[-1]["event_origin"] == "synthesized"


# --- attachment/file-history are out-of-scope here, unlike the personal ----
# --- adapter (round-2 correction: they can carry binary/file/diff content) -


def test_attachment_and_file_history_disposition_is_unsupported():
    """Direct assertion against the policy table — same reasoning as
    test_mode_permission_mode_disposition_is_preserved above: a record
    accidentally left classified `preserved` (the personal adapter's value)
    instead of reclassified `unsupported` here would still emit no event on
    its own account either way, so the behavioural test alone can't catch a
    silent revert of this table entry."""
    assert RAW_TYPE_DISPOSITION["attachment"] == UNSUPPORTED
    assert RAW_TYPE_DISPOSITION["file-history-snapshot"] == UNSUPPORTED
    assert RAW_TYPE_DISPOSITION["file-history-delta"] == UNSUPPORTED


def test_attachment_and_file_history_are_unsupported_not_preserved(result):
    assert result.raw_type_counts["attachment"] == 1
    assert result.raw_type_counts["file-history-snapshot"] == 1
    assert result.raw_type_counts["file-history-delta"] == 1
    # lines 14-16 must not have produced any canonical event
    for line in (14, 15, 16):
        assert [
            e for e in result.events
            if (e.get("raw_source_ref") or {}).get("line_or_offset") == line
        ] == []


def test_away_summary_and_local_command_subtype_disposition_is_unsupported():
    """Direct assertion against SYSTEM_SUBTYPE_DISPOSITION itself."""
    assert SYSTEM_SUBTYPE_DISPOSITION["away_summary"] == UNSUPPORTED
    assert SYSTEM_SUBTYPE_DISPOSITION["local_command"] == UNSUPPORTED


def test_away_summary_and_local_command_are_unsupported(result):
    # lines 17 (away_summary) and 18 (local_command) are both system
    # subtypes that can carry free-text content — out of scope here, unlike
    # turn_duration (line 7), which stays preserved.
    for line in (17, 18):
        assert [
            e for e in result.events
            if (e.get("raw_source_ref") or {}).get("line_or_offset") == line
        ] == []


def test_api_error_and_informational_absent_from_subtype_table():
    """`api_error`/`informational` are deliberately NOT classified — their
    managed environment content risk hasn't been reviewed, so they must fail closed
    (UnrecognizedRawType) rather than silently defaulting into either
    PRESERVED or UNSUPPORTED. This asserts the table's absence directly,
    since the runtime fail-closed test below only proves one such subtype
    (`informational`) actually raises — this proves the *reason* it raises
    is that both names are absent, not coincidence."""
    assert "api_error" not in SYSTEM_SUBTYPE_DISPOSITION
    assert "informational" not in SYSTEM_SUBTYPE_DISPOSITION


# --- unreviewed system subtypes fail closed (round-2 correction) -----------


def test_unrecognized_system_subtype_fails_closed():
    with pytest.raises(UnrecognizedRawType):
        normalize_managed_environment_session(
            UNKNOWN_SUBTYPE_FIXTURE,
            run_id=RUN_ID,
            local_secret=SECRET,
            repo_ref="sha256:fixture-managed-environment-repo-label",
        )


# --- optional promptSource (open contract gap, conservative default) -------


def test_prompt_source_present_recognized_opens_turn(result):
    user_messages = [e for e in result.events if e["event_type"] == "user_message"]
    sources = {e["user_message"]["prompt_source"] for e in user_messages}
    assert sources == {"typed", "system"}
    turns = {e["turn_index"] for e in user_messages}
    assert len(turns) == 2, "two recognized promptSource records must open two turns"


def test_prompt_source_absent_preserved_not_a_turn(result):
    # line 11 carries a promptId but no promptSource at all — classified
    # prompt_id_only_envelope (promptId present), same rule as the personal
    # adapter's already-reviewed 0.1.1 boundary. Either way, promptSource
    # being absent must never open a turn or emit user_message.
    assert result.preserved_detail.get("prompt_id_only_envelope", 0) == 1
    line_11_events = [
        e for e in result.events
        if (e.get("raw_source_ref") or {}).get("line_or_offset") == 11
    ]
    assert line_11_events == []


# --- orphan tool call / unknown tool-result outcome -------------------------


def test_orphan_tool_call_reported_never_synthesized(result):
    assert "toolu_ORPHAN" in result.unmatched_calls
    assert result.unmatched_results == []


def test_unknown_tool_result_outcome_is_unavailable_not_success(result):
    # line 5: toolu_A, is_error explicit false -> success observed False
    explicit = next(
        e for e in result.events
        if e["event_type"] == "tool_result"
        and (e.get("raw_source_ref") or {}).get("line_or_offset") == 5
    )
    assert explicit["tool_result"]["success"] is True
    assert explicit["field_status"]["success"] == "observed"

    # line 10: toolu_C, is_error absent entirely -> success unavailable, never guessed
    missing = next(
        e for e in result.events
        if e["event_type"] == "tool_result"
        and (e.get("raw_source_ref") or {}).get("line_or_offset") == 10
    )
    assert missing["tool_result"]["success"] is None
    assert missing["field_status"]["success"] == "unavailable"


# --- non-chronological timestamps -------------------------------------------


def test_non_chronological_timestamps_do_not_reorder_sequence_index(result):
    # line 6 (assistant, ts ...:09) is immediately followed in emission order
    # by line 8 (compact_boundary, ts ...:02) once line 7 (preserved) is
    # skipped — timestamp regresses while sequence_index still increases.
    by_line = {
        (e.get("raw_source_ref") or {}).get("line_or_offset"): e for e in result.events
    }
    assistant_ev = next(
        e for e in result.events
        if e["event_type"] == "assistant_message"
        and (e.get("raw_source_ref") or {}).get("line_or_offset") == 6
    )
    compaction_ev = by_line[8]
    assert compaction_ev["sequence_index"] > assistant_ev["sequence_index"]
    assert compaction_ev["timestamp"] < assistant_ev["timestamp"]


# --- parentUuid is never read or used --------------------------------------


def test_parent_uuid_never_referenced_in_adapter_source():
    """The module docstring discusses `parentUuid` in prose (backtick-quoted,
    explaining why it's unused) — this checks the actual dict-access form
    code would use to read it (`"parentUuid"`, double-quoted, as in
    `rec.get("parentUuid")` or `rec["parentUuid"]`), which must not appear
    anywhere, prose aside."""
    import agent_policy_lab.adapter_managed_environment_claude_code as mod

    source = Path(mod.__file__).read_text()
    assert '"parentUuid"' not in source


def test_invalid_parent_uuid_root_cardinality_present_but_unused(result):
    raw_records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    roots = [r for r in raw_records if r.get("parentUuid", "missing") is None]
    assert len(roots) >= 2, "fixture must exercise more than one parentUuid root"
    # Running normalization twice yields byte-identical event ids/order —
    # the malformed graph cannot have influenced anything, since nothing
    # reads it.
    again = normalize_managed_environment_session(
        FIXTURE,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
    )
    assert [e["event_id"] for e in again.events] == [e["event_id"] for e in result.events]


# --- archive cohort stays unavailable (open contract gap, gap probe) -------


def test_archive_trace_profile_unavailable_before_any_read():
    nonexistent = Path("/nonexistent/does-not-exist.jsonl")
    with pytest.raises(TraceProfileUnavailable):
        normalize_managed_environment_session(
            nonexistent,
            run_id=RUN_ID,
            local_secret=SECRET,
            repo_ref="sha256:fixture-managed-environment-repo-label",
            trace_profile="managed-environment-archived",
        )


# --- snapshot-mismatch refusal ----------------------------------------------


def test_reconcile_refuses_on_snapshot_mismatch(tmp_path):
    working_copy = tmp_path / "managed_environment_session_shapes.jsonl"
    working_copy.write_bytes(FIXTURE.read_bytes())

    snapshot = SessionSnapshot.capture(working_copy)
    outcome = normalize_managed_environment_session(
        snapshot,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
    )
    # File changes on disk between normalization and reconciliation.
    working_copy.write_bytes(FIXTURE.read_bytes() + b'{"type": "mode", "sessionId": "x", "mode": "default"}\n')

    with pytest.raises(SnapshotMismatch):
        reconcile(working_copy, outcome)


def test_reconcile_passes_per_class_on_unchanged_snapshot(tmp_path):
    working_copy = tmp_path / "managed_environment_session_shapes.jsonl"
    working_copy.write_bytes(FIXTURE.read_bytes())

    snapshot = SessionSnapshot.capture(working_copy)
    outcome = normalize_managed_environment_session(
        snapshot,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
    )
    report = reconcile(working_copy, outcome)
    for cls, row in report.items():
        if cls == "_preserved":
            assert row["ok"], row
            continue
        assert row["ok"], f"{cls}: {row}"


# --- privacy-field dropping --------------------------------------------------


def test_no_raw_content_or_identifiers_leak_into_events(result):
    serialized = json.dumps(result.events) + json.dumps(result.run)
    for marker in FORBIDDEN_SUBSTRINGS:
        assert marker not in serialized, f"forbidden raw content leaked: {marker!r}"


def test_no_raw_content_leaks_even_with_allowlists_and_repo_root(tmp_path):
    """The privacy check must hold even in the more permissive configuration
    (allowlists set, a real repo_root supplied) — not only in the maximally
    conservative default used by the other tests."""
    repo_root = tmp_path / "CORP" / "repo"
    (repo_root / "src").mkdir(parents=True)
    allowed = normalize_managed_environment_session(
        FIXTURE,
        run_id=RUN_ID,
        local_secret=SECRET,
        repo_ref="sha256:fixture-managed-environment-repo-label",
        tool_name_allowlist=frozenset({"Read", "Bash", "Glob"}),
        trigger_label_allowlist=frozenset({"auto"}),
        repo_root=repo_root,
    )
    serialized = json.dumps(allowed.events) + json.dumps(allowed.run)
    # cwd/gitBranch/message/thinking/model/tool-result content, and raw
    # tool-use ids, must still never appear — allowlisting tool NAMES and
    # the trigger label doesn't change any of that.
    for marker in FORBIDDEN_SUBSTRINGS:
        assert marker not in serialized, f"forbidden raw content leaked: {marker!r}"
