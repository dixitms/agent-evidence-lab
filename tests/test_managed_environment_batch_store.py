"""Tests for the managed environment-only batch staging/commit wrapper.

the managed-environment calibration runner design. Synthetic fixtures only —
reuses the already-committed `managed_environment_claude/` fixtures; no real
managed environment trace content anywhere.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from agent_evidence_lab.adapter_managed_environment_claude_code import (
    normalize_managed_environment_session,
    reconcile,
)
from agent_evidence_lab.managed_environment_batch_store import (
    ManagedEnvironmentBatchStore,
    ManagedEnvironmentBatchVerificationError,
    ManagedEnvironmentRunIdCollision,
    ManagedEnvironmentStoreFilesystemError,
)
from agent_evidence_lab.run_validation import validate_run
from agent_evidence_lab.store import Tier1PathError, build_manifest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "raw" / "managed_environment_claude" / "managed_environment_session_shapes.jsonl"
SECRET = b"fixture-local-secret-not-a-real-one"


def _stage_one_run(batch_store, batch_id, source_path, *, run_id):
    """Mirrors managed_environment_calibration_runner's phases 1-5 for one file,
    without going through the full runner — isolates ManagedEnvironmentBatchStore's
    own behavior from the orchestration module's."""
    from agent_evidence_lab.snapshot import SessionSnapshot

    snapshot = SessionSnapshot.capture(source_path)
    result = normalize_managed_environment_session(
        snapshot, run_id=run_id, local_secret=SECRET, repo_ref="sha256:fixture-repo",
    )
    reconciliation = reconcile(source_path, result)
    archived_path, archived_sha256 = batch_store.archive(batch_id, run_id, snapshot)
    manifest = build_manifest(
        result=result, reconciliation=reconciliation, raw_local_path=source_path,
        trace_profile="managed-environment-live-provisional",
        archived_transcript_path=str(archived_path),
        archived_transcript_sha256=archived_sha256,
    )
    rv = validate_run(result.events, result.run, manifest.to_dict())
    assert rv.ok, rv.summary()
    batch_store.stage(batch_id, events=result.events, run=result.run, manifest=manifest)
    return run_id


# --- construction and the same-filesystem guarantee -------------------------


def test_refuses_root_inside_the_repo():
    with pytest.raises(Tier1PathError):
        ManagedEnvironmentBatchStore(ROOT / "definitely-inside-the-repo")


def test_creates_mutable_roots_at_0700(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    for d in (store.runs_root, store.batches_dir, store.staging_base):
        assert oct(os.stat(d).st_mode)[-3:] == "700"


def test_same_filesystem_check_can_be_triggered(tmp_path, monkeypatch):
    import agent_evidence_lab.managed_environment_batch_store as mod

    real_stat = os.stat
    calls = {"n": 0}

    def fake_stat(path, *a, **kw):
        result = real_stat(path, *a, **kw)
        # Make the staging root report a different st_dev than everything else.
        if str(path).endswith("_staging"):
            class FakeStat:
                st_dev = result.st_dev + 1
            return FakeStat()
        return result

    monkeypatch.setattr(mod.os, "stat", fake_stat)
    with pytest.raises(ManagedEnvironmentStoreFilesystemError):
        ManagedEnvironmentBatchStore(tmp_path / "tier1")


def test_no_bypass_flag_exists_for_the_filesystem_check():
    import inspect

    from agent_evidence_lab.managed_environment_batch_store import ManagedEnvironmentBatchStore as CBS

    params = inspect.signature(CBS.__init__).parameters
    assert set(params) == {"self", "tier1_root"}, (
        "the filesystem check must not be configurable away — only tier1_root "
        "may be passed"
    )


# --- happy path: stage, commit, hardened, readable --------------------------


def test_stage_and_commit_round_trip(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "11111111-1111-4111-8111-111111111111"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)

    outcome = store.commit_batch(batch_id, [run_id])
    assert outcome.run_ids == [run_id]
    assert outcome.marker_path.exists()
    assert store.list_committed_runs() == [run_id]

    events, run, manifest = store.read_committed_run(run_id)
    assert manifest["trace_profile"] == "managed-environment-live-provisional"
    assert Path(manifest["archived_transcript_path"]).exists()
    # archived path was corrected to the FINAL location, not the staging one
    assert "_staging" not in manifest["archived_transcript_path"]

    # staging is cleaned up
    assert not (store.staging_base / batch_id).exists()


def test_committed_run_and_archive_are_hardened(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "22222222-2222-4222-8222-222222222222"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    store.commit_batch(batch_id, [run_id])

    run_dir = store.runs_root / run_id
    assert oct(os.stat(run_dir).st_mode)[-3:] == "500"
    for name in ("events.jsonl", "run.json", "manifest.json"):
        assert oct(os.stat(run_dir / name).st_mode)[-3:] == "400"
    raw_dir = run_dir / "raw"
    assert oct(os.stat(raw_dir).st_mode)[-3:] == "500"
    archive_file = next(raw_dir.iterdir())
    assert oct(os.stat(archive_file).st_mode)[-3:] == "400"

    # mutable roots are unaffected
    assert oct(os.stat(store.runs_root).st_mode)[-3:] == "700"
    assert oct(os.stat(store.batches_dir).st_mode)[-3:] == "700"


def test_committed_marker_is_0400_batches_dir_stays_0700(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "33333333-3333-4333-8333-333333333333"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    outcome = store.commit_batch(batch_id, [run_id])

    assert oct(os.stat(outcome.marker_path).st_mode)[-3:] == "400"
    assert oct(os.stat(store.batches_dir).st_mode)[-3:] == "700"


def test_marker_contains_four_hashes_per_run(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "44444444-4444-4444-8444-444444444444"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    outcome = store.commit_batch(batch_id, [run_id])

    marker = json.loads(outcome.marker_path.read_text())
    assert marker["batch_commit_version"] == "1.0.0"
    assert marker["batch_id"] == batch_id
    entry = marker["runs"][0]
    assert entry["run_id"] == run_id
    for key in ("events_sha256", "run_sha256", "manifest_sha256", "archived_transcript_sha256"):
        assert len(entry[key]) == 64


# --- fail-the-cohort atomicity -----------------------------------------------


def test_commit_rolls_back_when_second_run_fails_revalidation(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    good_run = "55555555-5555-4555-8555-555555555555"
    bad_run = "66666666-6666-4666-8666-666666666666"
    _stage_one_run(store, batch_id, FIXTURE, run_id=good_run)
    _stage_one_run(store, batch_id, FIXTURE, run_id=bad_run)

    # Corrupt the second run's staged manifest so post-rename re-validation
    # fails (schema_version pooled with an unknown version).
    staging_store = store.staging_store(batch_id)
    manifest_path = staging_store.run_dir(bad_run) / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["schema_version"] = "9.9.9"
    manifest_path.write_text(json.dumps(data))

    with pytest.raises(ManagedEnvironmentBatchVerificationError):
        store.commit_batch(batch_id, [good_run, bad_run])

    # Neither run is visible or even present in runs/ -- the good run was
    # renamed first, then rolled back when the bad run's re-validation failed.
    assert store.list_committed_runs() == []
    assert not (store.runs_root / good_run).exists()
    assert not (store.runs_root / bad_run).exists()


def test_rollback_batch_removes_staging_with_no_trace(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "77777777-7777-4777-8777-777777777777"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    assert (store.staging_base / batch_id).exists()

    store.rollback_batch(batch_id)
    assert not (store.staging_base / batch_id).exists()
    assert store.list_committed_runs() == []


# --- run_id collision --------------------------------------------------------


def test_run_id_in_use_true_for_committed_run(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "88888888-8888-4888-8888-888888888888"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    store.commit_batch(batch_id, [run_id])
    assert store.run_id_in_use(run_id) is True


def test_run_id_in_use_true_for_orphan_directory(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    orphan_id = "99999999-9999-4999-8999-999999999999"
    (store.runs_root / orphan_id).mkdir()
    assert store.run_id_in_use(orphan_id) is True
    # ...but it's still invisible as a committed run
    assert store.list_committed_runs() == []


def test_commit_refuses_a_run_id_that_collides_on_disk(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    run_id = "aaaaaaaa-1111-4111-8111-111111111111"
    (store.runs_root / run_id).mkdir()  # simulate an orphan already there

    batch_id = str(uuid.uuid4())
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    with pytest.raises(ManagedEnvironmentRunIdCollision):
        store.commit_batch(batch_id, [run_id])


# --- list_committed_runs() reader rejection rules ----------------------------


def test_list_committed_runs_rejects_tampered_committed_file(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "bbbbbbbb-1111-4111-8111-111111111111"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    store.commit_batch(batch_id, [run_id])
    assert store.list_committed_runs() == [run_id]

    run_json = store.runs_root / run_id / "run.json"
    os.chmod(run_json, 0o600)
    run_json.write_text(run_json.read_text() + " ")  # tamper after commit

    assert store.list_committed_runs() == []


def test_list_committed_runs_rejects_malformed_marker_json(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    (store.batches_dir / "garbage.json").write_text("{not valid json")
    assert store.list_committed_runs() == []


def test_list_committed_runs_rejects_marker_with_duplicate_run_id(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    run_id = "cccccccc-1111-4111-8111-111111111111"
    marker = {
        "batch_commit_version": "1.0.0",
        "batch_id": "b1",
        "committed_at": "2026-01-01T00:00:00+00:00",
        "runs": [
            {"run_id": run_id, "events_sha256": "a" * 64, "run_sha256": "a" * 64,
             "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64},
            {"run_id": run_id, "events_sha256": "b" * 64, "run_sha256": "b" * 64,
             "manifest_sha256": "b" * 64, "archived_transcript_sha256": "b" * 64},
        ],
    }
    (store.batches_dir / "dup.json").write_text(json.dumps(marker))
    assert store.list_committed_runs() == []


def test_list_committed_runs_rejects_unrecognized_batch_commit_version(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    run_id = "dddddddd-1111-4111-8111-111111111111"
    marker = {
        "batch_commit_version": "9.9.9",
        "batch_id": "b1",
        "committed_at": "2026-01-01T00:00:00+00:00",
        "runs": [{"run_id": run_id, "events_sha256": "a" * 64, "run_sha256": "a" * 64,
                  "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64}],
    }
    (store.batches_dir / "future.json").write_text(json.dumps(marker))
    assert store.list_committed_runs() == []


def test_list_committed_runs_ignores_orphan_directory_with_no_marker(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    orphan_id = "eeeeeeee-1111-4111-8111-111111111111"
    (store.runs_root / orphan_id).mkdir()
    (store.runs_root / orphan_id / "events.jsonl").write_text("")
    assert store.list_committed_runs() == []


def test_list_committed_runs_never_calls_generic_list_runs():
    """Structural guarantee, not just a behavioral one: nothing in this
    module's actual code may *call* `list_runs` as a method — several
    docstrings in this module discuss `Tier1Store.list_runs()` in prose
    while explaining why it's avoided, so this parses the AST and checks
    call sites specifically, rather than grepping text (which the prose
    would false-positive on)."""
    import ast

    import agent_evidence_lab.managed_environment_batch_store as mod

    tree = ast.parse(Path(mod.__file__).read_text())
    called_names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "list_runs" not in called_names


# --- round-6: secure roots before any write ----------------------------------


def test_fresh_tier1_root_is_created_at_0700(tmp_path):
    root = tmp_path / "tier1"
    assert not root.exists()
    ManagedEnvironmentBatchStore(root)
    assert oct(os.stat(root).st_mode)[-3:] == "700"


def test_construction_refuses_preexisting_root_with_group_bits(tmp_path):
    root = tmp_path / "tier1"
    root.mkdir()
    os.chmod(root, 0o755)  # mkdir's requested mode is filtered by umask
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not exactly 0700"):
        ManagedEnvironmentBatchStore(root)


def test_construction_refuses_preexisting_root_at_0600(tmp_path):
    """round-7: 0600 has no group/other bits but still isn't 0700 -- the
    original check (`mode & 0o077`) let this through."""
    root = tmp_path / "tier1"
    root.mkdir()
    os.chmod(root, 0o600)
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not exactly 0700"):
        ManagedEnvironmentBatchStore(root)


def test_construction_refuses_preexisting_root_at_0500(tmp_path):
    root = tmp_path / "tier1"
    root.mkdir()
    os.chmod(root, 0o500)
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not exactly 0700"):
        ManagedEnvironmentBatchStore(root)


def test_construction_refuses_a_regular_file_at_the_root_path(tmp_path):
    root = tmp_path / "tier1"
    root.write_text("not a directory")
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not a plain directory"):
        ManagedEnvironmentBatchStore(root)


def test_staging_batch_root_is_private_before_any_write(tmp_path):
    from agent_evidence_lab.snapshot import SessionSnapshot

    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "12121212-1212-4121-8121-121212121212"
    snapshot = SessionSnapshot.capture(FIXTURE)
    store.archive(batch_id, run_id, snapshot)  # the first write for this batch
    assert oct(os.stat(store.staging_base / batch_id).st_mode)[-3:] == "700"


def test_staging_store_refuses_preexisting_batch_root_with_group_bits(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    batch_root = store.staging_base / batch_id
    batch_root.mkdir()
    os.chmod(batch_root, 0o755)
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not exactly 0700"):
        store.staging_store(batch_id)


def test_staging_store_refuses_preexisting_batch_root_at_0500(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    batch_root = store.staging_base / batch_id
    batch_root.mkdir()
    os.chmod(batch_root, 0o500)
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not exactly 0700"):
        store.staging_store(batch_id)


def test_staging_store_refuses_a_regular_file_at_the_batch_root_path(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    (store.staging_base / batch_id).write_text("not a directory")
    with pytest.raises(ManagedEnvironmentStoreFilesystemError, match="not a plain directory"):
        store.staging_store(batch_id)


# --- round-6: complete-write loop for JSON writes ----------------------------


def test_short_writes_are_completed_not_truncated(tmp_path, monkeypatch):
    """os.write is not guaranteed to write everything in one call. Force
    every call to write at most 5 bytes and confirm the manifest correction
    and the marker -- both written via _atomic_write_json -- still land
    complete and valid."""
    import agent_evidence_lab.managed_environment_batch_store as mod

    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, bytes(data)[:5])

    monkeypatch.setattr(mod.os, "write", short_write)

    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "16161616-1616-4161-8161-161616161616"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    outcome = store.commit_batch(batch_id, [run_id])

    marker = json.loads(outcome.marker_path.read_text())
    assert marker["runs"][0]["run_id"] == run_id
    _, _, m = store.read_committed_run(run_id)
    assert m["trace_profile"] == "managed-environment-live-provisional"
    assert "_staging" not in m["archived_transcript_path"]


# --- round-6: rollback must work even after hardening ------------------------


def test_rollback_after_hardening_actually_removes_the_run(tmp_path, monkeypatch):
    """A failure discovered only at marker-hash time (after hardening has
    already run) must still result in the run directory being gone, not
    silently left behind because rmtree couldn't unlink into a 0500 dir."""
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "17171717-1717-4171-8171-171717171717"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)

    def boom(self, run_id):
        raise RuntimeError("simulated failure discovered after hardening")

    monkeypatch.setattr(ManagedEnvironmentBatchStore, "_marker_entry_for", boom)

    with pytest.raises(RuntimeError, match="simulated failure discovered after hardening"):
        store.commit_batch(batch_id, [run_id])

    assert not (store.runs_root / run_id).exists(), (
        "the hardened run directory must actually be removed, not left behind"
    )
    assert list(store.batches_dir.glob("*.json")) == []
    assert store.list_committed_runs() == []


# --- round-6: malformed marker entries never raise ---------------------------


def _valid_marker_skeleton() -> dict:
    """A marker that is valid in every respect EXCEPT whatever the caller
    overrides — isolates each malformation test to the one field it's
    actually testing, rather than accidentally also tripping over an
    invalid batch_id/committed_at at the same time."""
    return {
        "batch_commit_version": "1.0.0",
        "batch_id": str(uuid.uuid4()),
        "committed_at": "2026-01-01T00:00:00+00:00",
        "runs": [],
    }


@pytest.mark.parametrize("bad_entry", [
    "not-a-dict",
    123,
    None,
    {},
    {"run_id": "x"},
    {"run_id": 123, "events_sha256": "a" * 64, "run_sha256": "a" * 64,
     "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64},
    {"run_id": "x", "events_sha256": "a" * 64, "run_sha256": "a" * 64,
     "manifest_sha256": "a" * 64, "archived_transcript_sha256": "too-short"},
    {"run_id": "x", "events_sha256": "z" * 64, "run_sha256": "a" * 64,
     "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64},
    # round-7: uppercase hex must be rejected too -- this project never
    # emits it (hexdigest() is always lowercase), so accepting it would be
    # accidentally case-insensitive validation.
    {"run_id": "x", "events_sha256": "A" * 64, "run_sha256": "a" * 64,
     "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64},
])
def test_list_committed_runs_rejects_malformed_entry_without_raising(tmp_path, bad_entry):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    marker = _valid_marker_skeleton()
    marker["runs"] = [bad_entry]
    (store.batches_dir / "malformed.json").write_text(json.dumps(marker))
    assert store.list_committed_runs() == []  # must not raise


# --- round-7: the rest of the marker's own contract, isolated ---------------


@pytest.mark.parametrize("bad_batch_id", [
    "not-a-uuid",
    "12345678-1234-4123-8123",  # too short
    "00000000-0000-1000-8000-000000000000",  # a real UUID, but version 1, not 4
    "12345678-1234-4123-8123-1234567890AB",  # uppercase -- not canonical form
    123,
    None,
])
def test_list_committed_runs_rejects_non_uuid4_batch_id(tmp_path, bad_batch_id):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    run_id = str(uuid.uuid4())
    marker = _valid_marker_skeleton()
    marker["batch_id"] = bad_batch_id
    marker["runs"] = [{
        "run_id": run_id, "events_sha256": "a" * 64, "run_sha256": "a" * 64,
        "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64,
    }]
    (store.batches_dir / "bad-batch-id.json").write_text(json.dumps(marker))
    assert store.list_committed_runs() == []


@pytest.mark.parametrize("bad_committed_at", [
    "not a timestamp",
    "2026-01-01",  # date only, no time/offset
    "2026-01-01T00:00:00",  # naive -- no UTC offset at all
    "2026-01-01T00:00:00+05:00",  # a real offset, but not UTC
    123,
    None,
])
def test_list_committed_runs_rejects_non_utc_committed_at(tmp_path, bad_committed_at):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    run_id = str(uuid.uuid4())
    marker = _valid_marker_skeleton()
    marker["committed_at"] = bad_committed_at
    marker["runs"] = [{
        "run_id": run_id, "events_sha256": "a" * 64, "run_sha256": "a" * 64,
        "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64,
    }]
    (store.batches_dir / "bad-committed-at.json").write_text(json.dumps(marker))
    assert store.list_committed_runs() == []


def test_list_committed_runs_accepts_a_well_formed_but_otherwise_synthetic_marker(tmp_path):
    """Confirms the skeleton itself is genuinely valid — so the rejection
    tests above are proven to reject specifically the field under test, not
    something else in the skeleton that happens to also be wrong. This
    marker's run entry doesn't correspond to any real committed run, so it
    is correctly rejected too, but for a different, already-covered reason
    (missing run files) -- exercised here only to prove the marker-level
    fields (batch_commit_version, batch_id, committed_at) all parse
    correctly on their own via a run_id that plainly doesn't exist."""
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    marker = _valid_marker_skeleton()
    marker["runs"] = [{
        "run_id": str(uuid.uuid4()), "events_sha256": "a" * 64, "run_sha256": "a" * 64,
        "manifest_sha256": "a" * 64, "archived_transcript_sha256": "a" * 64,
    }]
    (store.batches_dir / "well-formed-but-orphan.json").write_text(json.dumps(marker))
    # Rejected because the referenced run doesn't exist on disk -- proves
    # marker-level parsing got that far, not that it failed earlier.
    assert store.list_committed_runs() == []


# --- round-6: read_committed_run is itself a managed environment read path -----------


def test_read_committed_run_refuses_an_orphan(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    orphan_id = "18181818-1818-4181-8181-181818181818"
    run_dir = store.runs_root / orphan_id
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text("")
    (run_dir / "run.json").write_text("{}")
    (run_dir / "manifest.json").write_text("{}")

    with pytest.raises(ManagedEnvironmentBatchVerificationError, match="not a committed run"):
        store.read_committed_run(orphan_id)


def test_read_committed_run_refuses_after_post_commit_tampering(tmp_path):
    store = ManagedEnvironmentBatchStore(tmp_path / "tier1")
    batch_id = str(uuid.uuid4())
    run_id = "19191919-1919-4191-8191-191919191919"
    _stage_one_run(store, batch_id, FIXTURE, run_id=run_id)
    store.commit_batch(batch_id, [run_id])
    store.read_committed_run(run_id)  # works before tampering

    run_json = store.runs_root / run_id / "run.json"
    os.chmod(run_json, 0o600)
    run_json.write_text(run_json.read_text() + " ")

    with pytest.raises(ManagedEnvironmentBatchVerificationError, match="not a committed run"):
        store.read_committed_run(run_id)
