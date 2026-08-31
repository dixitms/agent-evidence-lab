"""Local discovery runner: manifest validation, deterministic abort behavior,
and one synthetic happy-path integration test.

Fixture-only -- no real session is read, selected, or scanned anywhere in this
file. The integration test uses a small, hand-written, synthetic raw Claude
Code JSONL fixture (`fixtures/raw/local_discovery/`), not a real session.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import agent_evidence_lab.local_discovery_runner as runner_module
from agent_evidence_lab.local_discovery_diagnostics import DiagnosticSpec
from agent_evidence_lab.local_discovery_report import LocalDiscoveryDuplicateReport
from agent_evidence_lab.local_discovery_runner import (
    LocalDiscoveryManifestInvalid,
    LocalDiscoveryRunAborted,
    SessionManifestEntry,
    _require_all_reconciled,
    run,
    validate_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HAPPY_PATH_FIXTURE = (
    REPO_ROOT / "fixtures" / "raw" / "local_discovery" / "fixture-local-discovery-session-a.jsonl"
)


def _entry(tmp_path: Path, session_id: str, filename: str | None = None, content: str = "") -> SessionManifestEntry:
    path = tmp_path / (filename or f"{session_id}.jsonl")
    path.write_text(content)
    return SessionManifestEntry(session_id=session_id, source_path=path)


def _staged_happy_path_entry(tmp_path: Path) -> SessionManifestEntry:
    """The committed fixture lives inside the repo, and the manifest's own
    repo-boundary check (correctly) refuses a source_path inside it -- real
    session sources must live outside the working tree. Stage a copy under
    tmp_path (always outside the repo) before pointing a manifest at it,
    exactly as a real caller would point at a real, non-repo path."""
    staged = tmp_path / HAPPY_PATH_FIXTURE.name
    shutil.copyfile(HAPPY_PATH_FIXTURE, staged)
    return SessionManifestEntry(session_id="fixture-local-discovery-session-a", source_path=staged)


# ---------------------------------------------------------------------------
# Manifest validation: seven checks, in order, before any snapshotting
# ---------------------------------------------------------------------------


def test_validate_manifest_refuses_empty():
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([])


def test_validate_manifest_refuses_duplicate_session_id(tmp_path):
    a = _entry(tmp_path, "s1", filename="a.jsonl")
    b = _entry(tmp_path, "s1", filename="b.jsonl")
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([a, b])


def test_validate_manifest_refuses_duplicate_resolved_source_path(tmp_path):
    shared = tmp_path / "shared.jsonl"
    shared.write_text("")
    a = SessionManifestEntry(session_id="s1", source_path=shared)
    b = SessionManifestEntry(session_id="s2", source_path=Path(str(shared)))
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([a, b])


def test_validate_manifest_refuses_missing_file(tmp_path):
    entry = SessionManifestEntry(session_id="s1", source_path=tmp_path / "s1.jsonl")
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([entry])


def test_validate_manifest_refuses_a_directory(tmp_path):
    d = tmp_path / "s1.jsonl"
    d.mkdir()
    entry = SessionManifestEntry(session_id="s1", source_path=d)
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([entry])


def test_validate_manifest_refuses_non_jsonl_suffix(tmp_path):
    entry = _entry(tmp_path, "s1", filename="s1.json")
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([entry])


def test_validate_manifest_refuses_session_id_not_matching_filename_stem(tmp_path):
    entry = _entry(tmp_path, "declared-id", filename="different-stem.jsonl")
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([entry])


def test_validate_manifest_refuses_source_path_inside_repo():
    entry = SessionManifestEntry(
        session_id="scratch-inside-repo", source_path=REPO_ROOT / "scratch-inside-repo.jsonl"
    )
    with pytest.raises(LocalDiscoveryManifestInvalid):
        validate_manifest([entry])


def test_validate_manifest_accepts_a_valid_manifest(tmp_path):
    entry = _entry(tmp_path, "s1")
    validate_manifest([entry])  # must not raise


# ---------------------------------------------------------------------------
# Deterministic abort: unit level (pure function, no adapter/snapshot/file)
# ---------------------------------------------------------------------------


def test_require_all_reconciled_passes_when_everything_is_ok():
    _require_all_reconciled({"s1": {"tool_call": {"emitted": 1, "raw": 1, "ok": True}}})


def test_require_all_reconciled_aborts_on_any_mismatch():
    reconciliations = {
        "s1": {"tool_call": {"emitted": 1, "raw": 1, "ok": True}},
        "s2": {"tool_call": {"emitted": 1, "raw": 2, "ok": False}},
    }
    with pytest.raises(LocalDiscoveryRunAborted, match="s2"):
        _require_all_reconciled(reconciliations)


# ---------------------------------------------------------------------------
# Deterministic abort: orchestrator level, via a monkeypatched reconcile()
# result -- not "maybe a malformed JSONL happens to trigger a mismatch"
# ---------------------------------------------------------------------------


def test_run_aborts_when_reconcile_reports_a_mismatch(tmp_path, monkeypatch):
    """DiagnosticSpec is a frozen dataclass -- its own instances can't be
    monkeypatched in place. Instead, replace the runner's module-level
    DIAGNOSTICS_SPEC binding with fresh, call-tracking specs for this one
    test; `run()` looks up that module-global name at call time."""
    entry = _staged_happy_path_entry(tmp_path)

    calculate_calls: list[str] = []

    def _make_tracking_spec(name: str) -> DiagnosticSpec:
        def _tracking_calculate(ctx, _name=name):
            calculate_calls.append(_name)
            return {"outcome": "computed", "value": {}, "provenance": {"rule_version": "1.0.0"}}

        def _tracking_aggregate(per_session, _name=name):
            return {"outcome": "computed", "value": {}, "provenance": {"rule_version": "1.0.0"}}

        return DiagnosticSpec(
            name=name, measure_kind="derived", rule_version="1.0.0", signal_vocabulary=(),
            calculate=_tracking_calculate, aggregate=_tracking_aggregate,
        )

    fake_specs = tuple(_make_tracking_spec(d.name) for d in runner_module.DIAGNOSTICS_SPEC)
    monkeypatch.setattr(runner_module, "DIAGNOSTICS_SPEC", fake_specs)

    def _broken_reconcile(snapshot, result):
        return {"tool_call": {"emitted": 1, "raw": 2, "ok": False}}

    monkeypatch.setattr(runner_module.adapter_claude_code, "reconcile", _broken_reconcile)

    root = tmp_path / "tier1" / "local_discovery"
    with pytest.raises(LocalDiscoveryRunAborted):
        run([entry], report_id="ldr-abort-test", root=root)

    assert calculate_calls == []  # no diagnostic calculator was ever called
    assert not (root / "ldr-abort-test.json").exists()  # no report file
    assert not (root / "_index" / "ldr-abort-test").exists()  # no index entry


# ---------------------------------------------------------------------------
# Synthetic happy-path integration test
# ---------------------------------------------------------------------------


def test_run_end_to_end_against_synthetic_raw_jsonl(tmp_path):
    entry = _staged_happy_path_entry(tmp_path)
    root = tmp_path / "tier1" / "local_discovery"

    written = run([entry], report_id="ldr-happy-path", root=root, label="synthetic happy path")

    report = json.loads(written.read_text())
    assert report["scope"] == {
        "subject_count": 1,
        "repository_count": 1,
        "session_count": 1,
        "source_client": "claude-code",
        "label": "synthetic happy path",
    }
    assert len(report["input_sessions"]) == 1
    assert report["input_sessions"][0]["session_id"] == "fixture-local-discovery-session-a"

    results = report["results"]
    sid = "fixture-local-discovery-session-a"

    # hand-verified against the fixture (see the fixture itself for the
    # reproduction commands used to derive these numbers independently)
    assert results["compaction_count"]["per_session"][sid]["value"] == {"count": 0}
    assert results["compaction_count"]["aggregate"]["value"] == {"total_count": 0}
    # event_type is a required envelope classification, not a payload
    # field_status -- the end-to-end report must carry the corrected shape too
    assert results["compaction_count"]["per_session"][sid]["provenance"]["inputs"] == [
        {"field": "event_type", "basis": "required_envelope_classification"}
    ]

    assert results["assistant_token_usage"]["per_session"][sid]["value"] == {
        "input_tokens": 23, "output_tokens": 80,
    }
    assert results["assistant_token_usage"]["aggregate"]["value"] == {
        "total_input_tokens": 23, "total_output_tokens": 80,
    }

    assert results["tool_invocation_distribution"]["per_session"][sid]["value"] == {
        "Read": 1, "Bash": 1,
    }
    assert results["tool_invocation_distribution"]["aggregate"]["value"] == {
        "Read": 1, "Bash": 1,
    }

    assert results["paired_tool_outcome_observed_success"]["per_session"][sid]["value"] == {
        "observed_success": 1, "observed_failure": 1,
    }
    assert results["paired_tool_outcome_observed_success"]["aggregate"]["value"] == {
        "observed_success": 1, "observed_failure": 1,
    }

    assert results["elapsed_event_span"]["per_session"][sid]["value"] == {"elapsed_seconds": 20}
    assert results["elapsed_event_span"]["aggregate"]["value"] == {"total_elapsed_seconds": 20}
    # timestamp is a bare envelope field, evidenced by presence/absence,
    # never a borrowed payload field_status
    for inp in results["elapsed_event_span"]["per_session"][sid]["provenance"]["inputs"]:
        assert inp["basis"] == "envelope_timestamp_presence"
        assert "field_status" not in inp

    recon = results["reconciliation_availability"]["per_session"][sid]["value"]
    assert recon["tool_call"] == {"raw_count": 2, "canonical_count": 2, "delta": 0}
    assert recon["tool_result"] == {"raw_count": 2, "canonical_count": 2, "delta": 0}
    assert recon["assistant_message"] == {"raw_count": 3, "canonical_count": 3, "delta": 0}
    assert "ok" not in recon

    # every availability tuple reconciles to the single-session scope
    for name, entry_availability in report["availability"].items():
        tup = entry_availability["sessions"]
        assert tup["considered"] == 1
        assert tup["computed"] + tup["not_reportable"] + tup["excluded"] == 1


def test_run_refuses_duplicate_report_id_reusing_the_writer_guarantee(tmp_path):
    entry = _staged_happy_path_entry(tmp_path)
    root = tmp_path / "tier1" / "local_discovery"
    run([entry], report_id="ldr-dup-test", root=root)
    with pytest.raises(LocalDiscoveryDuplicateReport):
        run([entry], report_id="ldr-dup-test", root=root)
