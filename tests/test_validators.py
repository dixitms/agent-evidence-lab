"""Negative tests for the dataset validators.

Every check here is exercised by deliberately corrupting valid adapter output.
A validator that has never been watched to fail is indistinguishable from one
that does nothing — the same discipline the fixture set applies to the
schema's conditionals.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from agent_policy_lab.adapter_claude_code import normalize_session
from agent_policy_lab.validators import validate_dataset

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "fixtures" / "raw" / "session_shapes.jsonl"
RUN_ID = "6f1d3a2e-0c4b-4f8a-9e21-5b7c8d9e0a11"


@pytest.fixture
def clean():
    r = normalize_session(
        RAW, run_id=RUN_ID, repo_root="/REPO",
        local_secret=b"fixture-secret", repo_ref="sha256:fixture-repo",
    )
    return copy.deepcopy(r.events), [copy.deepcopy(r.run)]


def test_clean_dataset_passes(clean):
    events, runs = clean
    assert validate_dataset(events, runs).ok


def test_duplicate_sequence_index_is_caught(clean):
    events, runs = clean
    events[3]["sequence_index"] = events[2]["sequence_index"]
    report = validate_dataset(events, runs)
    assert not report.ok
    assert any("duplicate sequence_index" in f for f in report.findings)


def test_dangling_run_id_is_caught(clean):
    events, runs = clean
    events[1]["run_id"] = "00000000-0000-4000-8000-000000000000"
    report = validate_dataset(events, runs)
    assert not report.ok
    assert any("dangling run_id" in f for f in report.findings)


def test_payload_field_without_field_status_is_caught(clean):
    events, runs = clean
    call = next(e for e in events if e["event_type"] == "tool_call")
    del call["field_status"]["target_path"]
    report = validate_dataset(events, runs)
    assert not report.ok
    assert any("no field_status entry" in f for f in report.findings)


def test_field_status_declaring_absent_field_is_caught(clean):
    events, runs = clean
    call = next(e for e in events if e["event_type"] == "tool_call")
    call["field_status"]["invented_field"] = "observed"
    report = validate_dataset(events, runs)
    assert not report.ok
    assert any("not present in any payload" in f for f in report.findings)


def test_duplicate_assistant_envelope_is_caught(clean):
    events, runs = clean
    envelope = next(e for e in events if e["event_type"] == "assistant_message")
    dupe = copy.deepcopy(envelope)
    dupe["sequence_index"] = 9999
    dupe["event_id"] = "11111111-2222-4333-8444-555555555555"
    events.append(dupe)
    report = validate_dataset(events, runs)
    assert not report.ok
    assert any("assistant_message envelopes" in f for f in report.findings)


def test_unmatched_counts_are_reported_not_findings(clean):
    """An orphan call is a fact to report, not a data error — silence is the failure."""
    events, runs = clean
    report = validate_dataset(events, runs)
    assert report.ok, "the fixture's orphan call must not be a finding"
    assert report.unmatched_calls == 1
    assert report.unmatched_results == 0
