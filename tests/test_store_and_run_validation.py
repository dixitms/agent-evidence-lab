"""Tier 1 store, run manifest, and whole-run validation.

Every failure path is exercised deliberately. A gate nobody has watched reject
something is indistinguishable from one that does nothing — the same discipline
the schema fixtures apply, applied to the run level.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_policy_lab.adapter_claude_code import normalize_session as claude_normalize
from agent_policy_lab.adapter_claude_code import reconcile as claude_reconcile
from agent_policy_lab.adapter_codex import normalize_session as codex_normalize
from agent_policy_lab.adapter_codex import reconcile as codex_reconcile
from agent_policy_lab.run_validation import (
    UnknownSchemaVersion,
    validate_run,
    validate_stored_run,
)
from agent_policy_lab.snapshot import SessionSnapshot
from agent_policy_lab.store import (
    Tier1PathError,
    Tier1Store,
    build_manifest,
    sha256_text,
)

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_RAW = ROOT / "fixtures" / "raw" / "session_shapes.jsonl"
CODEX_RAW = ROOT / "fixtures" / "raw" / "codex" / "session_shapes.jsonl"
RUN_ID = "6f1d3a2e-0c4b-4f8a-9e21-5b7c8d9e0a11"
SECRET = b"store-test-secret"


def _claude_run():
    snap = SessionSnapshot.capture(CLAUDE_RAW)
    result = claude_normalize(snap, run_id=RUN_ID, repo_root="/REPO",
                              local_secret=SECRET, repo_ref="sha256:fixture")
    rec = claude_reconcile(snap, result)
    manifest = build_manifest(
        result=result, reconciliation=rec, raw_local_path=CLAUDE_RAW,
        task_id="apl-d-t1", policy_id=None, policy_version=None,
        policy_text_sha256=None, task_prompt_sha256=sha256_text("frozen prompt"),
    )
    return result, rec, manifest


def _codex_run():
    snap = SessionSnapshot.capture(CODEX_RAW)
    result = codex_normalize(snap, run_id="7a2e4b3f-1d5c-4a9b-8f32-6c8d9e0a1b22",
                             local_secret=SECRET, repo_ref="sha256:fixture")
    rec = codex_reconcile(snap, result)
    manifest = build_manifest(result=result, reconciliation=rec, raw_local_path=CODEX_RAW)
    return result, rec, manifest


# --- Tier 1 boundary -----------------------------------------------------


def test_store_refuses_to_live_inside_the_repo():
    with pytest.raises(Tier1PathError, match="inside the repository"):
        Tier1Store(ROOT / "raw")


def test_store_accepts_a_path_outside_the_repo(tmp_path):
    store = Tier1Store(tmp_path / "apl")
    assert store.root == (tmp_path / "apl").resolve()


# --- round trip ----------------------------------------------------------


def test_write_and_read_round_trip(tmp_path):
    result, rec, manifest = _claude_run()
    store = Tier1Store(tmp_path)
    store.write_run(events=result.events, run=result.run, manifest=manifest)

    events, run, m = store.read_run(RUN_ID)
    assert len(events) == len(result.events)
    assert run["run_id"] == RUN_ID
    assert m["manifest_version"] == "1.4.0"
    assert store.list_runs() == [RUN_ID]


def test_manifest_carries_provenance_not_content(tmp_path):
    _, _, manifest = _claude_run()
    blob = json.dumps(manifest.to_dict())
    # identity, hashes and counts — never prompts, messages or tool output
    assert manifest.session_snapshot_sha256 and len(manifest.session_snapshot_sha256) == 64
    assert manifest.task_prompt_sha256 and len(manifest.task_prompt_sha256) == 64
    assert "frozen prompt" not in blob, "the prompt itself must not be in the manifest"
    assert manifest.event_count == len(_claude_run()[0].events)
    assert manifest.reconciled_ok is True


def test_write_refuses_a_manifest_that_miscounts(tmp_path):
    result, _, manifest = _claude_run()
    manifest.event_count += 1
    with pytest.raises(ValueError, match="miscounts"):
        Tier1Store(tmp_path).write_run(
            events=result.events, run=result.run, manifest=manifest)


# --- whole-run validation ------------------------------------------------


def test_validated_run_passes_all_four_checks(tmp_path):
    result, _, manifest = _claude_run()
    store = Tier1Store(tmp_path)
    store.write_run(events=result.events, run=result.run, manifest=manifest)
    rv = validate_stored_run(store, RUN_ID)
    assert rv.ok, rv.summary()
    assert "validated" in rv.summary()


def test_codex_run_validates_against_v02(tmp_path):
    result, _, manifest = _codex_run()
    store = Tier1Store(tmp_path)
    store.write_run(events=result.events, run=result.run, manifest=manifest)
    rv = validate_stored_run(store, result.run["run_id"])
    assert rv.ok, rv.summary()
    assert manifest.schema_version == "0.2.0"


def test_schema_violation_is_caught(tmp_path):
    result, _, manifest = _claude_run()
    events = [dict(e) for e in result.events]
    events[2] = {**events[2], "event_type": "not_a_real_type"}
    rv = validate_run(events, result.run, manifest.to_dict())
    assert not rv.ok and rv.schema_errors


def test_dataset_finding_is_caught(tmp_path):
    result, _, manifest = _claude_run()
    events = [dict(e) for e in result.events]
    events[3] = {**events[3], "sequence_index": events[2]["sequence_index"]}
    rv = validate_run(events, result.run, manifest.to_dict())
    assert not rv.ok and any("duplicate sequence_index" in f for f in rv.dataset_findings)


def test_failed_reconciliation_is_caught(tmp_path):
    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["reconciliation"]["tool_call"] = {"emitted": 1, "raw": 9, "ok": False}
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok and rv.reconciliation_failures


def test_missing_reconciliation_is_not_silently_accepted(tmp_path):
    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["reconciliation"] = {}
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok
    assert any("no reconciliation report" in f for f in rv.reconciliation_failures)


def test_manifest_disagreement_is_caught(tmp_path):
    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["pinned_commit"] = "0000000000000000000000000000000000000000"
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok and any("pinned_commit" in d for d in rv.manifest_disagreements)


def test_mixed_contract_versions_in_one_run_are_refused(tmp_path):
    """0.1.1 and 0.2.0 must never be pooled, including inside a single run."""
    result, _, manifest = _claude_run()
    events = [dict(e) for e in result.events]
    events[1] = {**events[1], "schema_version": "0.2.0"}
    rv = validate_run(events, result.run, manifest.to_dict())
    assert not rv.ok
    assert any("never pooled" in e for e in rv.schema_errors)


def test_unknown_contract_version_fails_loudly(tmp_path):
    result, _, manifest = _claude_run()
    events = [dict(e) for e in result.events]
    events[0] = {**events[0], "schema_version": "9.9.9"}
    with pytest.raises(UnknownSchemaVersion, match="9.9.9"):
        validate_run(events, result.run, manifest.to_dict())


# --- manifest_version is a contract in store validation too --------------


@pytest.mark.parametrize("bad", ["2.0.0", "1.0", "", None, "v1"])
def test_run_validation_rejects_unsupported_manifest_version(bad):
    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["manifest_version"] = bad
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok
    assert any("manifest_version" in d for d in rv.manifest_disagreements)


# --- transcript archival: provenance must not depend on a file we don't own ---
# ~/.claude/projects is the client's; it may rotate or compact. A manifest
# pointing only there has an expiry date nobody controls.


def test_transcript_is_archived_and_made_read_only(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "session.jsonl"
    src.write_text('{"type":"user"}\n')
    dest, digest = store.archive_transcript(RUN_ID, src)
    assert dest.exists() and dest.read_text() == src.read_text()
    assert len(digest) == 64
    assert oct(dest.stat().st_mode)[-3:] == "444", "read-only, so overwriting is deliberate"


def test_archiving_the_same_bytes_twice_is_idempotent(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "s.jsonl"; src.write_text("x\n")
    a, d1 = store.archive_transcript(RUN_ID, src)
    b, d2 = store.archive_transcript(RUN_ID, src)
    assert a == b and d1 == d2


def test_archiving_different_bytes_under_one_run_is_refused(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "s.jsonl"; src.write_text("first\n")
    store.archive_transcript(RUN_ID, src)
    src.write_text("second\n")
    with pytest.raises(ValueError, match="does not change after the fact"):
        store.archive_transcript(RUN_ID, src)


def test_survives_deletion_of_the_client_side_transcript(tmp_path):
    """The point of archiving: the run stays verifiable when the original goes."""
    store = Tier1Store(tmp_path)
    client_side = tmp_path / "claude-projects" / "sess.jsonl"
    client_side.parent.mkdir()
    client_side.write_text('{"type":"user"}\n')
    dest, digest = store.archive_transcript(RUN_ID, client_side)

    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["archived_transcript_path"] = str(dest)
    m["archived_transcript_sha256"] = digest
    store.write_run(events=result.events, run=result.run,
                    manifest=type(manifest)(**{**manifest.to_dict(),
                                               "archived_transcript_path": str(dest),
                                               "archived_transcript_sha256": digest}))
    client_side.unlink()                      # the client rotates its logs
    assert store.verify_archive(RUN_ID) is None, "still verifiable"


def test_validation_catches_a_missing_archive(tmp_path):
    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["archived_transcript_path"] = str(tmp_path / "gone.jsonl")
    m["archived_transcript_sha256"] = "a" * 64
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok and any("no longer be re-verified" in d
                             for d in rv.manifest_disagreements)


def test_validation_catches_a_tampered_archive(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "s.jsonl"; src.write_text("original\n")
    dest, digest = store.archive_transcript(RUN_ID, src)
    dest.chmod(0o644); dest.write_text("tampered\n")

    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["archived_transcript_path"] = str(dest)
    m["archived_transcript_sha256"] = digest
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok and any("has changed since ingestion" in d
                             for d in rv.manifest_disagreements)


def test_half_declared_archive_is_refused(tmp_path):
    result, _, manifest = _claude_run()
    m = manifest.to_dict()
    m["archived_transcript_path"] = "/somewhere/x.jsonl"   # hash omitted
    rv = validate_run(result.events, result.run, m)
    assert not rv.ok and any("half-declared" in d for d in rv.manifest_disagreements)


# --- snapshot-based archival: no fresh disk read, unlike archive_transcript ---
# (archive-then-manifest ordering)


def test_snapshot_is_archived_from_captured_bytes_and_made_read_only(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "session.jsonl"
    src.write_text('{"type":"user"}\n')
    snap = SessionSnapshot.capture(src)
    dest, digest = store.archive_snapshot(RUN_ID, snap)
    assert dest.exists() and dest.read_bytes() == snap.content
    assert digest == snap.sha256
    assert oct(dest.stat().st_mode)[-3:] == "444"


def test_archive_snapshot_ignores_a_live_change_after_capture(tmp_path):
    """The whole point: unlike archive_transcript, archiving must reflect the
    bytes captured at Phase 1, not whatever the live file says by the time
    archival runs."""
    store = Tier1Store(tmp_path)
    src = tmp_path / "session.jsonl"
    src.write_text("original\n")
    snap = SessionSnapshot.capture(src)
    src.write_text("changed after capture\n")  # live file mutates
    dest, digest = store.archive_snapshot(RUN_ID, snap)
    assert dest.read_text() == "original\n"
    assert digest == snap.sha256


def test_archiving_the_same_snapshot_twice_is_idempotent(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "s.jsonl"; src.write_text("x\n")
    snap = SessionSnapshot.capture(src)
    a, d1 = store.archive_snapshot(RUN_ID, snap)
    b, d2 = store.archive_snapshot(RUN_ID, snap)
    assert a == b and d1 == d2


def test_archiving_a_different_snapshot_under_one_run_is_refused(tmp_path):
    store = Tier1Store(tmp_path)
    src = tmp_path / "s.jsonl"
    src.write_text("first\n")
    store.archive_snapshot(RUN_ID, SessionSnapshot.capture(src))
    src.write_text("second\n")
    with pytest.raises(ValueError, match="does not change after the fact"):
        store.archive_snapshot(RUN_ID, SessionSnapshot.capture(src))


# --- trace_profile (manifest 1.4.0) --------------------------------------


def test_trace_profile_defaults_to_none_and_round_trips(tmp_path):
    result, rec, manifest = _claude_run()
    assert manifest.trace_profile is None
    assert manifest.manifest_version == "1.4.0"

    managed_environment_manifest = build_manifest(
        result=result, reconciliation=rec, raw_local_path="/x.jsonl",
        trace_profile="managed-environment-live-provisional",
    )
    assert managed_environment_manifest.trace_profile == "managed-environment-live-provisional"

    store = Tier1Store(tmp_path)
    store.write_run(events=result.events, run=result.run, manifest=managed_environment_manifest)
    _, _, m = store.read_run(RUN_ID)
    assert m["trace_profile"] == "managed-environment-live-provisional"
