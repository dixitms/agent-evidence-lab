"""Tests for the managed environment calibration runner orchestration.

the managed-environment calibration runner design. Reuses the already-committed
`managed_environment_claude/` fixtures — one that normalizes cleanly and one that
deliberately raises `UnrecognizedRawType` — to prove fail-the-cohort
atomicity, not just the happy path.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from agent_policy_lab.managed_environment_batch_store import ManagedEnvironmentBatchStore
from agent_policy_lab.managed_environment_calibration_runner import (
    IntakeManifestError,
    load_intake_manifest,
    run_calibration_batch,
)
from agent_policy_lab.snapshot import SnapshotMismatch

ROOT = Path(__file__).resolve().parent.parent
GOOD_FIXTURE = ROOT / "fixtures" / "raw" / "managed_environment_claude" / "managed_environment_session_shapes.jsonl"
BAD_FIXTURE = (
    ROOT / "fixtures" / "raw" / "managed_environment_claude"
    / "managed_environment_session_unknown_system_subtype.jsonl"
)
SECRET = b"fixture-local-secret-not-a-real-one"


def _write_intake_manifest(path: Path, files: list[Path]) -> Path:
    path.write_text(json.dumps({
        "manifest_format_version": "1.0.0",
        "files": [str(f) for f in files],
    }))
    path.chmod(0o600)
    return path


# --- intake manifest validation ----------------------------------------------


def test_load_intake_manifest_valid(tmp_path):
    work = tmp_path / "s.jsonl"
    shutil.copy(GOOD_FIXTURE, work)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [work])
    assert load_intake_manifest(manifest) == [work]


def test_load_intake_manifest_rejects_insecure_permissions(tmp_path):
    work = tmp_path / "s.jsonl"
    shutil.copy(GOOD_FIXTURE, work)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [work])
    manifest.chmod(0o644)  # group/other readable
    with pytest.raises(IntakeManifestError, match="not private enough"):
        load_intake_manifest(manifest)


def test_load_intake_manifest_rejects_unknown_version(tmp_path):
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps({"manifest_format_version": "9.9.9", "files": ["/x"]}))
    manifest.chmod(0o600)
    with pytest.raises(IntakeManifestError, match="unrecognized manifest_format_version"):
        load_intake_manifest(manifest)


def test_load_intake_manifest_rejects_relative_path(tmp_path):
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps({
        "manifest_format_version": "1.0.0", "files": ["relative/path.jsonl"],
    }))
    manifest.chmod(0o600)
    with pytest.raises(IntakeManifestError, match="not an absolute path"):
        load_intake_manifest(manifest)


def test_load_intake_manifest_rejects_directory_entry(tmp_path):
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps({
        "manifest_format_version": "1.0.0", "files": [str(a_dir)],
    }))
    manifest.chmod(0o600)
    with pytest.raises(IntakeManifestError, match="resolves to a directory"):
        load_intake_manifest(manifest)


def test_load_intake_manifest_rejects_duplicate_path(tmp_path):
    work = tmp_path / "s.jsonl"
    shutil.copy(GOOD_FIXTURE, work)
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps({
        "manifest_format_version": "1.0.0", "files": [str(work), str(work)],
    }))
    manifest.chmod(0o600)
    with pytest.raises(IntakeManifestError, match="duplicate path"):
        load_intake_manifest(manifest)


def test_load_intake_manifest_rejects_empty_files_list(tmp_path):
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps({"manifest_format_version": "1.0.0", "files": []}))
    manifest.chmod(0o600)
    with pytest.raises(IntakeManifestError, match="non-empty list"):
        load_intake_manifest(manifest)


def test_load_intake_manifest_rejects_non_object(tmp_path):
    manifest = tmp_path / "intake.json"
    manifest.write_text(json.dumps(["not", "an", "object"]))
    manifest.chmod(0o600)
    with pytest.raises(IntakeManifestError, match="must be a JSON object"):
        load_intake_manifest(manifest)


# --- happy path ---------------------------------------------------------------


def test_successful_batch_commits_and_is_readable(tmp_path):
    work = tmp_path / "s.jsonl"
    shutil.copy(GOOD_FIXTURE, work)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [work])
    tier1_root = tmp_path / "tier1"

    outcome = run_calibration_batch(
        manifest, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert outcome.committed
    assert len(outcome.run_ids) == 1
    assert outcome.marker_path.exists()

    store = ManagedEnvironmentBatchStore(tier1_root)
    assert store.list_committed_runs() == outcome.run_ids
    events, run, m = store.read_committed_run(outcome.run_ids[0])
    assert run["run_kind"] == "calibration"
    assert run["disposition"] == "excluded"
    assert m["trace_profile"] == "managed-environment-live-provisional"


def test_successful_batch_with_multiple_files(tmp_path):
    work1 = tmp_path / "s1.jsonl"
    work2 = tmp_path / "s2.jsonl"
    shutil.copy(GOOD_FIXTURE, work1)
    shutil.copy(GOOD_FIXTURE, work2)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [work1, work2])
    tier1_root = tmp_path / "tier1"

    outcome = run_calibration_batch(
        manifest, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert outcome.committed
    assert len(outcome.run_ids) == 2
    assert len(set(outcome.run_ids)) == 2  # distinct run_ids

    store = ManagedEnvironmentBatchStore(tier1_root)
    assert sorted(store.list_committed_runs()) == sorted(outcome.run_ids)


# --- fail-the-cohort: unknown raw type/subtype fails closed, whole batch ----


def test_unrecognized_subtype_fails_the_whole_batch(tmp_path):
    good = tmp_path / "good.jsonl"
    bad = tmp_path / "bad.jsonl"
    shutil.copy(GOOD_FIXTURE, good)
    shutil.copy(BAD_FIXTURE, bad)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [good, bad])
    tier1_root = tmp_path / "tier1"

    outcome = run_calibration_batch(
        manifest, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert not outcome.committed
    assert outcome.run_ids == []
    assert outcome.failure_reason

    store = ManagedEnvironmentBatchStore(tier1_root)
    assert store.list_committed_runs() == [], (
        "the GOOD file's run must not be committed either — one bad file "
        "in the batch refuses the whole cohort, atomically"
    )
    # nothing left behind in staging
    assert list(store.staging_base.iterdir()) == []


def test_bad_file_alone_also_fails_closed(tmp_path):
    bad = tmp_path / "bad.jsonl"
    shutil.copy(BAD_FIXTURE, bad)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [bad])
    tier1_root = tmp_path / "tier1"

    outcome = run_calibration_batch(
        manifest, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert not outcome.committed
    assert "UnrecognizedRawType" in outcome.failure_reason or "subtype" in outcome.failure_reason


# --- snapshot drift fails the whole batch (via monkeypatch) -----------------


def test_snapshot_drift_during_reconcile_fails_the_batch(tmp_path, monkeypatch):
    import agent_policy_lab.managed_environment_calibration_runner as mod

    good1 = tmp_path / "s1.jsonl"
    good2 = tmp_path / "s2.jsonl"
    shutil.copy(GOOD_FIXTURE, good1)
    shutil.copy(GOOD_FIXTURE, good2)
    manifest = _write_intake_manifest(tmp_path / "intake.json", [good1, good2])
    tier1_root = tmp_path / "tier1"

    real_reconcile = mod.reconcile
    call_count = {"n": 0}

    def flaky_reconcile(source, result):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise SnapshotMismatch("simulated drift on the second file")
        return real_reconcile(source, result)

    monkeypatch.setattr(mod, "reconcile", flaky_reconcile)

    outcome = run_calibration_batch(
        manifest, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert not outcome.committed
    assert "simulated drift" in outcome.failure_reason

    store = ManagedEnvironmentBatchStore(tier1_root)
    assert store.list_committed_runs() == [], (
        "the first file's run must not survive either -- drift detected on "
        "the second file refuses the whole batch"
    )


# --- run_id collision across separate batches --------------------------------


def test_run_id_collision_across_batches_refuses_the_second(tmp_path, monkeypatch):
    import agent_policy_lab.managed_environment_calibration_runner as mod

    forced_run_id = uuid.UUID("12345678-1234-4123-8123-123456789012")
    forced_batch_id_1 = uuid.UUID("aaaaaaaa-1234-4123-8123-123456789012")
    forced_batch_id_2 = uuid.UUID("bbbbbbbb-1234-4123-8123-123456789012")
    sequence = iter([forced_batch_id_1, forced_run_id, forced_batch_id_2, forced_run_id])
    monkeypatch.setattr(mod.uuid, "uuid4", lambda: next(sequence))

    tier1_root = tmp_path / "tier1"

    work1 = tmp_path / "s1.jsonl"
    shutil.copy(GOOD_FIXTURE, work1)
    manifest1 = _write_intake_manifest(tmp_path / "intake1.json", [work1])
    outcome1 = run_calibration_batch(
        manifest1, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert outcome1.committed
    assert outcome1.run_ids == [str(forced_run_id)]

    work2 = tmp_path / "s2.jsonl"
    shutil.copy(GOOD_FIXTURE, work2)
    manifest2 = _write_intake_manifest(tmp_path / "intake2.json", [work2])
    outcome2 = run_calibration_batch(
        manifest2, tier1_root=tier1_root, local_secret=SECRET,
        repo_ref="sha256:fixture-repo",
    )
    assert not outcome2.committed
    assert "already in use" in outcome2.failure_reason

    store = ManagedEnvironmentBatchStore(tier1_root)
    assert store.list_committed_runs() == [str(forced_run_id)], (
        "the first batch's committed run must be undisturbed by the second "
        "batch's refusal"
    )


# --- non-detector, non-report scope --------------------------------------


def test_runner_module_has_no_detector_or_report_imports():
    """Scope guarantee: this module must not import anything from a
    detector or report module -- the whole point of the provisional
    profile is that nothing analyzes its output yet."""
    import agent_policy_lab.managed_environment_calibration_runner as mod

    source = Path(mod.__file__).read_text()
    for forbidden in ("detector_", "_report", "export_validator"):
        assert forbidden not in source, f"unexpected reference to {forbidden!r}"


def test_trace_profile_is_not_a_parameter():
    """The runner must not expose a way to request any profile other than
    the one supported one -- confirmed structurally, not just by trusting
    the hardcoded default stays hardcoded."""
    import inspect

    from agent_policy_lab.managed_environment_calibration_runner import run_calibration_batch

    params = inspect.signature(run_calibration_batch).parameters
    assert "trace_profile" not in params
