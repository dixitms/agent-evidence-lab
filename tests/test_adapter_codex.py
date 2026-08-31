"""Codex adapter tests against synthetic rollout fixtures.

Fixtures are hand-written and shape-accurate; no real transcript content
appears in them (Tier 1 raw is never committed).

The three tests Codex named as gate conditions are marked below:
  (G-a) no double counting from item_completed
  (G-b) no fabricated per-message token attribution
  (G-c) opaque reasoning stays unavailable
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_evidence_lab.adapter_codex import (
    MissingAnchorCommit,
    UnrecognizedRawType,
    normalize_session,
    raw_class_counts,
    reconcile,
)
from agent_evidence_lab.snapshot import SessionSnapshot, SnapshotMismatch
from agent_evidence_lab.validators import validate_dataset

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "fixtures" / "raw" / "codex"
RUN_ID = "7a2e4b3f-1d5c-4a9b-8f32-6c8d9e0a1b22"
SECRET = b"codex-fixture-secret-not-a-real-one"


@pytest.fixture(scope="module")
def event_schema():
    return Draft202012Validator(
        json.loads((ROOT / "schema/v0.2/agent_event.schema.json").read_text())
    )


@pytest.fixture(scope="module")
def run_schema():
    return Draft202012Validator(
        json.loads((ROOT / "schema/v0.2/experiment_run.schema.json").read_text())
    )


@pytest.fixture(scope="module")
def result():
    return normalize_session(
        RAW / "session_shapes.jsonl",
        run_id=RUN_ID, local_secret=SECRET, repo_ref="sha256:codex-fixture",
    )


# --- contract ------------------------------------------------------------


def test_every_event_validates_against_v02(result, event_schema):
    for ev in result.events:
        errors = sorted(event_schema.iter_errors(ev), key=lambda e: list(e.path))
        assert not errors, (
            f"{ev['event_type']} seq={ev['sequence_index']}: "
            + "; ".join(e.message for e in errors[:3])
        )


def test_events_declare_v02_not_v01(result):
    assert {e["schema_version"] for e in result.events} == {"0.2.0"}
    assert result.run["schema_version"] == "0.2.0"


def test_run_record_validates(result, run_schema):
    errors = sorted(run_schema.iter_errors(result.run), key=lambda e: list(e.path))
    assert not errors, "; ".join(e.message for e in errors)


def test_dataset_validators_pass(result):
    report = validate_dataset(result.events, [result.run])
    assert report.ok, "\n".join(report.findings)


# --- gate: per-class reconciliation against the authoritative stream -----


def test_per_class_reconciliation(result):
    rep = reconcile(RAW / "session_shapes.jsonl", result)
    bad = {k: v for k, v in rep.items() if not v.get("ok")}
    assert not bad, f"classes failed reconciliation: {bad}"


def test_reconciliation_is_per_class_not_an_aggregate(result):
    """A compensating error must fail, which an aggregate ceiling would not.

    Drop one tool_call and add one thinking: the total is unchanged, so a
    total-event ceiling would pass. Per-class reconciliation must not.
    """
    events = [dict(e) for e in result.events]
    idx = next(i for i, e in enumerate(events) if e["event_type"] == "tool_call")
    events[idx] = {**events[idx], "event_type": "thinking"}
    doctored = type(result)(
        events=events, run=result.run,
        snapshot_sha256=result.snapshot_sha256,
        disposition_counts=result.disposition_counts,
        preserved_detail=result.preserved_detail,
        wrapper_counts=result.wrapper_counts,
        unmatched_calls=result.unmatched_calls,
        unmatched_results=result.unmatched_results,
    )
    rep = reconcile(RAW / "session_shapes.jsonl", doctored)
    assert len(doctored.events) == len(result.events), "total deliberately unchanged"
    assert not rep["tool_call"]["ok"] and not rep["thinking"]["ok"]


# --- (G-a) no double counting from item_completed ------------------------


def test_item_completed_is_preserved_never_normalized(result):
    raw = raw_class_counts(RAW / "session_shapes.jsonl")
    assert raw["item_completed"] == 5, "fixture must contain item_completed records"
    assert result.preserved_detail["semantic_projection_item_completed"] == 5
    # every canonical event traces to a response_item / compacted / session line,
    # never to an item_completed line
    ic_lines = {
        i
        for i, line in enumerate(
            (RAW / "session_shapes.jsonl").read_text().splitlines(), start=1
        )
        if (json.loads(line).get("payload") or {}).get("type") == "item_completed"
    }
    for ev in result.events:
        ref = ev.get("raw_source_ref") or {}
        assert ref.get("line_or_offset") not in ic_lines, (
            f"{ev['event_type']} was normalized from an item_completed line"
        )


def test_no_double_count_of_tool_activity(result):
    """item_completed carries CommandExecution/FileChange for the same work."""
    raw = raw_class_counts(RAW / "session_shapes.jsonl")
    emitted = sum(1 for e in result.events if e["event_type"] == "tool_call")
    assert emitted == raw["tool_call"] == 2
    # the fixture's CommandExecution + FileChange items must not have inflated it
    assert emitted < raw["item_completed"]


def test_developer_message_preserved_not_normalized(result):
    assert result.preserved_detail["message_role_developer"] == 1
    roles_emitted = [e["event_type"] for e in result.events]
    assert roles_emitted.count("user_message") == 4  # 3 in turn 0 + 1 in turn 1
    assert roles_emitted.count("assistant_message") == 1


# --- (G-b) no fabricated per-message token attribution -------------------


def test_no_token_attribution_from_cumulative_stream(result):
    envelopes = [e for e in result.events if e["event_type"] == "assistant_message"]
    assert envelopes
    for ev in envelopes:
        assert ev["assistant_message"] == {
            "input_tokens": None, "output_tokens": None,
            "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
        }
        assert set(ev["field_status"].values()) == {"unavailable"}


def test_token_count_stream_is_preserved_not_emitted(result):
    assert result.preserved_detail["cumulative_token_stream"] == 1
    assert all(e["event_type"] != "token_count" for e in result.events)


# --- (G-c) opaque reasoning stays unavailable ----------------------------


def test_reasoning_is_emitted_but_length_unavailable(result):
    thinking = [e for e in result.events if e["event_type"] == "thinking"]
    assert len(thinking) == 1, "presence of reasoning IS observed"
    assert thinking[0]["thinking"]["length_value"] is None
    assert thinking[0]["field_status"]["length_value"] == "unavailable"
    assert thinking[0]["event_origin"] == "observed"


def test_ciphertext_length_never_leaks_into_the_event(result):
    """The fixture's encrypted_content is 1804 chars; that must appear nowhere."""
    blob = json.dumps(result.events)
    assert "1804" not in blob
    assert "ZZZZ" not in blob


# --- turns, correlation, provenance --------------------------------------


def test_turns_come_from_task_started_not_user_messages(result):
    raw = raw_class_counts(RAW / "session_shapes.jsonl")
    assert raw["turns"] == 2
    assert max(e["turn_index"] for e in result.events) + 1 == 2
    # four user messages across two turns — counts deliberately diverge
    assert raw["user_message"] == 4
    turn0 = [e for e in result.events
             if e["event_type"] == "user_message" and e["turn_index"] == 0]
    assert len(turn0) == 3, "three protocol-level user messages in one turn is normal"


def test_correlation_ids_are_hmac_not_raw(result):
    for ev in result.events:
        payload = ev.get("tool_call") or ev.get("tool_result") or {}
        cid = payload.get("tool_correlation_id")
        if cid:
            assert cid not in {"call_A", "call_B"}
            assert len(cid) == 64
    calls = {e["tool_call"]["tool_correlation_id"]
             for e in result.events if e["event_type"] == "tool_call"}
    results = {e["tool_result"]["tool_correlation_id"]
               for e in result.events if e["event_type"] == "tool_result"}
    assert calls == results and len(calls) == 2
    assert not result.unmatched_calls and not result.unmatched_results


def test_session_start_observed_session_end_synthesized(result):
    first, last = result.events[0], result.events[-1]
    assert first["event_type"] == "session_start"
    assert first["event_origin"] == "observed"  # native session_meta
    assert first["raw_source_ref"] is not None
    assert last["event_type"] == "session_end"
    assert last["event_origin"] == "synthesized" and last["synthesis_reason"]


def test_compaction_observed_but_metrics_unavailable(result):
    comps = [e for e in result.events if e["event_type"] == "compaction"]
    assert len(comps) == 1
    c = comps[0]
    assert c["event_origin"] == "observed", "we observed that compaction occurred"
    assert c["compaction"]["compaction_source"] == "unavailable"
    assert all(c["compaction"][k] is None for k in
               ("trigger", "pre_tokens", "post_tokens",
                "cumulative_dropped_tokens", "duration_ms"))


def test_tool_result_success_is_unavailable_not_guessed(result):
    for ev in result.events:
        if ev["event_type"] == "tool_result":
            assert ev["tool_result"]["success"] is None
            assert ev["field_status"]["success"] == "unavailable"


def test_function_call_without_input_has_unavailable_digest(result):
    wait = [e for e in result.events
            if e["event_type"] == "tool_call" and e["tool_call"]["tool_name"] == "wait"][0]
    assert wait["tool_call"]["args_digest"] is None
    assert wait["field_status"]["args_digest"] == "unavailable"


def test_target_path_always_unavailable_for_codex(result):
    for ev in result.events:
        if ev["event_type"] == "tool_call":
            assert ev["tool_call"]["target_path"] is None
            assert ev["field_status"]["target_path"] == "unavailable"


# --- calibration boundary and fail-loudly --------------------------------


def test_calibration_run_is_excluded_from_evidence(result):
    assert result.run["run_kind"] == "calibration"
    assert result.run["disposition"] == "excluded"
    assert "calibration only, never evidence" in result.run["disposition_reason"]


def test_session_without_commit_is_refused():
    with pytest.raises(MissingAnchorCommit, match="commit_hash"):
        normalize_session(RAW / "session_no_commit.jsonl", run_id=RUN_ID,
                          local_secret=SECRET, repo_ref="x")


def test_unknown_event_msg_subtype_fails_loudly():
    with pytest.raises(UnrecognizedRawType, match="brand_new_codex_subtype"):
        normalize_session(RAW / "session_unknown_subtype.jsonl", run_id=RUN_ID,
                          local_secret=SECRET, repo_ref="x")


def test_reingestion_is_deterministic():
    a = normalize_session(RAW / "session_shapes.jsonl", run_id=RUN_ID,
                          local_secret=SECRET, repo_ref="sha256:codex-fixture")
    b = normalize_session(RAW / "session_shapes.jsonl", run_id=RUN_ID,
                          local_secret=SECRET, repo_ref="sha256:codex-fixture")
    assert a.events == b.events


def test_every_raw_line_lands_in_a_disposition(result):
    total = sum(result.disposition_counts.values())
    raw_lines = len((RAW / "session_shapes.jsonl").read_text().strip().splitlines())
    assert total == raw_lines


# --- snapshot contract: the gate cannot compare two file states ----------


def test_append_between_normalize_and_reconcile_is_refused(tmp_path):
    """The live-corpus hazard, made mechanical rather than procedural.

    Both clients' session files grow while in use. Normalizing one read and
    reconciling a second read of the same path silently compares two different
    file states — and a reconciliation gate that can be fooled that way is
    worse than none, because it reports "ok".
    """
    live = tmp_path / "live.jsonl"
    live.write_bytes((RAW / "session_shapes.jsonl").read_bytes())

    result = normalize_session(live, run_id=RUN_ID, local_secret=SECRET,
                               repo_ref="sha256:codex-fixture")
    assert reconcile(live, result), "clean reconcile before any append"

    # a Codex session appends a record mid-run, exactly as observed live
    with live.open("a") as fh:
        fh.write(json.dumps({
            "type": "response_item", "timestamp": "2026-08-18T17:00:00.000Z",
            "payload": {"type": "reasoning", "id": "rsX", "summary": [],
                        "encrypted_content": "Q" * 64,
                        "internal_chat_message_metadata_passthrough": None},
        }) + "\n")

    with pytest.raises(SnapshotMismatch, match="changed between normalization"):
        reconcile(live, result)


def test_snapshot_object_carries_through_both_steps(tmp_path):
    """Passing one snapshot to both steps is immune to appends by construction."""
    live = tmp_path / "live.jsonl"
    live.write_bytes((RAW / "session_shapes.jsonl").read_bytes())
    snap = SessionSnapshot.capture(live)

    result = normalize_session(snap, run_id=RUN_ID, local_secret=SECRET,
                               repo_ref="sha256:codex-fixture")
    with live.open("a") as fh:
        fh.write('{"type":"world_state","payload":{},"timestamp":"2026-08-18T17:01:00.000Z"}\n')

    # the file moved on; the snapshot did not, so the comparison stays valid
    rep = reconcile(snap, result)
    assert all(v.get("ok") for v in rep.values())
    assert result.snapshot_sha256 == snap.sha256
    assert SessionSnapshot.capture(live).sha256 != snap.sha256


def test_result_records_the_hash_it_normalized(result):
    expected = SessionSnapshot.capture(RAW / "session_shapes.jsonl").sha256
    assert result.snapshot_sha256 == expected
    assert len(result.snapshot_sha256) == 64
