"""Adapter tests against synthetic raw fixtures.

The raw fixtures are hand-written and shape-accurate; no real transcript
content appears in them (Tier 1 raw is never committed —
constraint 2). See fixtures/raw/README.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_evidence_lab.adapter_claude_code import (
    UnrecognizedRawType,
    normalize_session,
    raw_class_counts,
    reconcile,
)
from agent_evidence_lab.validators import validate_dataset

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "fixtures" / "raw"
RUN_ID = "6f1d3a2e-0c4b-4f8a-9e21-5b7c8d9e0a11"
SECRET = b"fixture-local-secret-not-a-real-one"


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
    return normalize_session(
        RAW / "session_shapes.jsonl",
        run_id=RUN_ID,
        repo_root="/REPO",
        local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )


# --- the gate: adapter output validates against the frozen schema ---------


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


# --- exactly one envelope per normalized assistant line (the half the -----
# --- dataset validator cannot check, because it needs the raw input) ------


def test_exactly_one_assistant_envelope_per_assistant_line(result):
    raw_assistant_lines = {
        i
        for i, line in enumerate(
            (RAW / "session_shapes.jsonl").read_text().splitlines(), start=1
        )
        if json.loads(line).get("type") == "assistant"
    }
    envelope_lines = [
        ev["raw_source_ref"]["line_or_offset"]
        for ev in result.events
        if ev["event_type"] == "assistant_message"
    ]
    assert sorted(envelope_lines) == sorted(raw_assistant_lines)
    assert len(envelope_lines) == len(set(envelope_lines)), "duplicate envelope"


def test_envelope_carries_usage_and_blocks_do_not(result):
    envelopes = [e for e in result.events if e["event_type"] == "assistant_message"]
    assert envelopes
    for ev in envelopes:
        # usage describes the whole response, so it points at no single block
        assert ev["raw_source_ref"]["json_pointer"] is None
    for ev in result.events:
        if ev["event_type"] in {"thinking", "tool_call"}:
            assert "assistant_message" not in ev


def test_multi_block_response_yields_four_events(result):
    """[thinking, text, tool_use] + envelope == 4 events, not 5: text folds in."""
    line4 = [
        e
        for e in result.events
        if (e.get("raw_source_ref") or {}).get("line_or_offset") == 4
    ]
    assert sorted(e["event_type"] for e in line4) == [
        "assistant_message",
        "thinking",
        "tool_call",
    ]


# --- determinism ----------------------------------------------------------


def test_reingestion_is_byte_identical():
    a = normalize_session(
        RAW / "session_shapes.jsonl", run_id=RUN_ID, repo_root="/REPO",
        local_secret=SECRET, repo_ref="sha256:fixture-repo",
    )
    b = normalize_session(
        RAW / "session_shapes.jsonl", run_id=RUN_ID, repo_root="/REPO",
        local_secret=SECRET, repo_ref="sha256:fixture-repo",
    )
    assert a.events == b.events, "re-ingestion must be deterministic"


def test_correlation_id_is_not_the_raw_id(result):
    raw_ids = {"toolu_A", "toolu_B", "toolu_C", "toolu_ORPHAN"}
    for ev in result.events:
        payload = ev.get("tool_call") or ev.get("tool_result") or {}
        cid = payload.get("tool_correlation_id")
        if cid:
            assert cid not in raw_ids, "raw client id must never be published"
            assert len(cid) == 64


def test_correlation_links_call_to_result(result):
    calls = {
        e["tool_call"]["tool_correlation_id"]
        for e in result.events
        if e["event_type"] == "tool_call"
    }
    results = {
        e["tool_result"]["tool_correlation_id"]
        for e in result.events
        if e["event_type"] == "tool_result"
    }
    assert len(calls & results) == 4, "four calls in the fixture are answered"


def test_orphan_call_reported_not_invented(result):
    assert len(result.unmatched_calls) == 1
    assert not result.unmatched_results
    # the orphan produced no placeholder result event
    assert sum(1 for e in result.events if e["event_type"] == "tool_result") == 4


# --- dispositions --------------------------------------------------------


def test_disposition_counts_cover_every_raw_line(result):
    total = sum(result.disposition_counts.values())
    raw_lines = len((RAW / "session_shapes.jsonl").read_text().strip().splitlines())
    assert total == raw_lines, "every raw line must land in exactly one disposition"
    # 6 user + 4 assistant + 2 compact_boundary; the third `system` line is
    # turn_duration, reclassified to preserved rather than normalized.
    assert result.disposition_counts["normalized"] == 13
    # 5 non-normalized raw types, plus 4 user records reclassified out of
    # `normalized`: one isMeta injection, two promptId-only envelopes, and
    # one user record without a prompt source or prompt id.
    assert result.disposition_counts["preserved-local-not-normalized"] == 9
    assert result.disposition_counts["unsupported"] == 5
    assert result.preserved_detail == {
        "meta_injection": 1,
        "prompt_id_only_envelope": 2,
        "user_record_without_prompt_source": 1,
    }


def test_unrecognized_raw_type_fails_loudly():
    with pytest.raises(UnrecognizedRawType, match="brand-new-client-thing"):
        normalize_session(
            RAW / "session_unknown_type.jsonl", run_id=RUN_ID, repo_root="/REPO",
            local_secret=SECRET, repo_ref="sha256:fixture-repo",
        )


# --- turn_index / prompt_source ------------------------------------------


def test_system_prompt_opens_a_turn(result):
    user_msgs = [e for e in result.events if e["event_type"] == "user_message"]
    assert [e["user_message"]["prompt_source"] for e in user_msgs] == ["typed", "system"]
    assert [e["turn_index"] for e in user_msgs] == [0, 1]


def test_tool_result_carrier_does_not_open_a_turn(result):
    assert max(e["turn_index"] for e in result.events) == 1


def test_tool_result_carrier_with_promptid_still_yields_results(result):
    """Regression: promptId is NOT the turn discriminator.

    Real sessions carry a promptId on tool_result carriers (9,965 across the
    local corpus). Gating result extraction on its absence silently dropped
    391 of 494 results in the first spot-checked session — the adapter looked
    correct and simply lost most of the data.
    """
    import json as _json

    raw = [
        _json.loads(l)
        for l in (RAW / "session_shapes.jsonl").read_text().splitlines()
    ]
    carriers_with_pid = [
        i
        for i, r in enumerate(raw, start=1)
        if r.get("type") == "user"
        and r.get("promptId")
        and isinstance(r.get("message", {}).get("content"), list)
        and any(b.get("type") == "tool_result" for b in r["message"]["content"])
    ]
    assert carriers_with_pid, "fixture must cover the promptId-bearing carrier"
    result_lines = {
        e["raw_source_ref"]["line_or_offset"]
        for e in result.events
        if e["event_type"] == "tool_result"
    }
    for line in carriers_with_pid:
        assert line in result_lines, f"line {line} carried a tool_result that was dropped"


def test_prompt_id_only_envelope_is_preserved_not_a_turn(result):
    """A promptId without a recognized promptSource is not
    an agent input cycle. It is retained locally under a distinct preserved
    reason, emits no user_message, adds no turn, and is never smoothed into
    prompt_source: null.
    """
    assert result.preserved_detail.get("prompt_id_only_envelope") == 2
    # lines 22 and 23 are the interruption marker and the slash-command envelope
    evented_lines = {
        e["raw_source_ref"]["line_or_offset"]
        for e in result.events
        if e.get("raw_source_ref")
    }
    assert 22 not in evented_lines and 23 not in evented_lines
    sources = [
        e["user_message"]["prompt_source"]
        for e in result.events
        if e["event_type"] == "user_message"
    ]
    assert None not in sources, "no envelope may become prompt_source: null"
    assert set(sources) <= {"typed", "suggestion_accepted", "system", "sdk"}


def test_prompt_id_only_envelope_still_yields_its_tool_results(result):
    """The preserved path must not repeat the 391-result loss."""
    result_lines = {
        e["raw_source_ref"]["line_or_offset"]
        for e in result.events
        if e["event_type"] == "tool_result"
    }
    assert 24 in result_lines, "promptId-only carrier lost its tool_result"


def test_user_record_without_prompt_source_is_preserved_and_reconciled(result):
    raw = raw_class_counts(RAW / "session_shapes.jsonl")
    report = reconcile(RAW / "session_shapes.jsonl", result)
    assert raw["user_record_without_prompt_source"] == 1
    assert result.preserved_detail["user_record_without_prompt_source"] == 1
    assert report["_preserved"]["user_record_without_prompt_source_raw"] == 1
    assert report["_preserved"]["user_record_without_prompt_source_preserved"] == 1
    assert report["_preserved"]["ok"]


def test_meta_record_is_not_a_turn_and_emits_no_event(result):
    meta_lines = {
        e["raw_source_ref"]["line_or_offset"]
        for e in result.events
        if e.get("raw_source_ref")
    }
    assert 18 not in meta_lines, "isMeta harness injection must not become an event"


# --- payload specifics ---------------------------------------------------


def test_target_path_is_repo_relative_and_outside_repo_is_unavailable(result):
    # Keyed by line, not tool_name: the fixture has two Read calls on purpose
    # (one inside the repo, one outside), and a name-keyed dict hides one.
    by_line = {
        (e["raw_source_ref"]["line_or_offset"], e["tool_call"]["tool_name"]): e
        for e in result.events
        if e["event_type"] == "tool_call"
    }
    inside = by_line[(4, "Read")]
    assert inside["tool_call"]["target_path"] == "src/router.py"
    assert not inside["tool_call"]["target_path"].startswith("/")
    assert inside["field_status"]["target_path"] == "observed"
    outside = by_line[(16, "Read")]
    assert outside["tool_call"]["target_path"] is None
    assert outside["field_status"]["target_path"] == "unavailable"


def test_repo_root_none_forces_every_target_path_unavailable_without_resolution():
    # repo_root=None is the explicit no-usable-root mode. Line 4's
    # /REPO/src/router.py would resolve fine under a real root (proven by
    # the test above) -- with repo_root=None it must still come back
    # unavailable, not attempt resolution and get lucky or unlucky.
    r = normalize_session(
        RAW / "session_shapes.jsonl", run_id=RUN_ID, repo_root=None,
        local_secret=SECRET, repo_ref="sha256:fixture",
    )
    tool_calls = [e for e in r.events if e["event_type"] == "tool_call"
                 and e["tool_call"]["tool_name"] in ("Read", "Edit", "Write", "MultiEdit")]
    assert tool_calls, "fixture must exercise at least one file-tool call"
    for e in tool_calls:
        assert e["tool_call"]["target_path"] is None
        assert e["field_status"]["target_path"] == "unavailable"


def test_result_size_counts_utf8_bytes_for_string_and_array(result):
    results = {
        e["raw_source_ref"]["line_or_offset"]: e["tool_result"]
        for e in result.events
        if e["event_type"] == "tool_result"
    }
    assert results[5]["result_size_value"] == len("line one\nline two".encode())
    assert results[7]["result_size_value"] == len("FAILED 1 error".encode())
    assert all(r["result_size_unit"] == "utf8_bytes" for r in results.values())


def test_success_unavailable_when_is_error_absent(result):
    absent = [
        e
        for e in result.events
        if e["event_type"] == "tool_result" and e["raw_source_ref"]["line_or_offset"] == 17
    ][0]
    assert absent["tool_result"]["success"] is None
    assert absent["field_status"]["success"] == "unavailable"


def test_compaction_both_sources_and_missing_cumulative(result):
    comps = [e["compaction"] for e in result.events if e["event_type"] == "compaction"]
    sources = [c["compaction_source"] for c in comps]
    assert sources.count("boundary_marker") == 2
    assert sources.count("continuation_summary") == 1
    older = [c for c in comps if c["trigger"] == "manual"][0]
    assert older["cumulative_dropped_tokens"] is None
    older_ev = [
        e
        for e in result.events
        if e["event_type"] == "compaction"
        and (e.get("compaction") or {}).get("trigger") == "manual"
    ][0]
    assert older_ev["field_status"]["cumulative_dropped_tokens"] == "unavailable"


def test_session_boundaries_are_synthesized(result):
    first, last = result.events[0], result.events[-1]
    assert first["event_type"] == "session_start"
    assert last["event_type"] == "session_end"
    for ev in (first, last):
        assert ev["event_origin"] == "synthesized"
        assert ev["synthesis_reason"]
        assert ev["raw_source_ref"] is None


# --- model handling ------------------------------------------------------


def test_synthetic_model_never_becomes_the_run_model(result):
    assert result.run["model"] == "claude-sonnet-5"
    assert result.run["model_source"] == "observed_from_trace"
    assert result.run["model_evidence_consistent"] is True


# --- per-class reconciliation (parity with the Codex adapter) ------------


def test_per_class_reconciliation(result):
    from agent_evidence_lab.adapter_claude_code import reconcile
    rep = reconcile(RAW / "session_shapes.jsonl", result)
    bad = {k: v for k, v in rep.items() if not v.get("ok")}
    assert not bad, f"classes failed reconciliation: {bad}"


def test_reconciliation_catches_a_compensating_error(result):
    """The exact shape an aggregate ceiling misses."""
    from agent_evidence_lab.adapter_claude_code import reconcile
    events = [dict(e) for e in result.events]
    idx = next(i for i, e in enumerate(events) if e["event_type"] == "tool_result")
    events[idx] = {**events[idx], "event_type": "thinking"}
    doctored = type(result)(
        events=events, run=result.run, snapshot_sha256=result.snapshot_sha256,
        disposition_counts=result.disposition_counts,
        preserved_detail=result.preserved_detail,
        raw_type_counts=result.raw_type_counts,
        unmatched_calls=result.unmatched_calls,
        unmatched_results=result.unmatched_results,
    )
    rep = reconcile(RAW / "session_shapes.jsonl", doctored)
    assert len(doctored.events) == len(result.events)
    assert not rep["tool_result"]["ok"] and not rep["thinking"]["ok"]


def test_reconcile_refuses_a_file_that_grew(tmp_path):
    """A live session file grew 898 -> 1,074 lines while it was being read."""
    from agent_evidence_lab.adapter_claude_code import reconcile
    from agent_evidence_lab.snapshot import SnapshotMismatch

    live = tmp_path / "live.jsonl"
    live.write_bytes((RAW / "session_shapes.jsonl").read_bytes())
    r = normalize_session(live, run_id=RUN_ID, repo_root="/REPO",
                          local_secret=SECRET, repo_ref="sha256:fixture")
    assert reconcile(live, r)

    with live.open("a") as fh:
        fh.write(json.dumps({
            "type": "user", "sessionId": "s", "cwd": "/REPO", "version": "2.1.220",
            "uuid": "uX", "parentUuid": None, "promptId": "pX", "promptSource": "typed",
            "origin": {"kind": "human"}, "timestamp": "2026-08-19T10:00:00.000Z",
            "message": {"role": "user", "content": "another turn"},
        }) + "\n")

    with pytest.raises(SnapshotMismatch):
        reconcile(live, r)


def test_preserved_streams_reconcile_too(result):
    from agent_evidence_lab.adapter_claude_code import raw_class_counts, reconcile
    raw = raw_class_counts(RAW / "session_shapes.jsonl")
    rep = reconcile(RAW / "session_shapes.jsonl", result)
    assert rep["_preserved"]["ok"]
    assert raw["prompt_id_only_envelope"] == 2 and raw["meta_injection"] == 1
