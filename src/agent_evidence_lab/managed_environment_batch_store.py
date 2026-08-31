"""Managed-environment batch staging and commit around `Tier1Store`.

the managed-environment calibration runner design, settled after five review
rounds. `Tier1Store.write_run()` alone writes one run at a time with no
cross-run atomicity — fine for the personal adapter's use, wrong for a
calibration batch that must be all-or-nothing. This module adds exactly
that, as an **additive, opt-in** wrapper: nothing here changes
`Tier1Store`'s behavior for any existing caller that never constructs this
class.

Every run in a batch stages into `<tier1_root>/_staging/<batch_id>/` (via an
ordinary `Tier1Store` rooted there — `write_run`/`read_run` are reused
completely unchanged), gets read back and verified, then — only if every
run in the batch cleared every gate — commits: rename into
`<tier1_root>/runs/<run_id>`, correct `manifest.json`'s archived-transcript
path in place now that it's true, re-validate, harden permissions, fsync
everything, and only then write a durable batch-commit marker. The marker's
existence, not a bare `runs/<run_id>` directory, is what makes a run valid
— see `list_committed_runs()`. Nothing here ever calls the generic
`Tier1Store.list_runs()`; a bare directory listing has no marker validation
and would defeat every guarantee this module exists to provide.

Fsync fences throughout (Phase 6): an atomic `os.rename` only
guarantees the rename itself is all-or-nothing, not that renamed content,
an in-place edit, or a permission change is durable yet. Every `fsync`
below closes exactly one such gap.

Round-6 hardening, found on implementation review: `tier1_root` and every
`_staging/<batch_id>` root are now required to be `0700` (created fresh, or
refused if a pre-existing directory has any group/other bit) *before* any
archive or event write happens under them, not only their `runs/`/
`_batches/`/`_staging/` children. JSON writes loop until every byte is
actually written, since a single `os.write` call is not guaranteed to write
everything requested. A rollback attempted after a run has already been
hardened restores owner write/traverse first, or `rmtree` cannot actually
remove what it's asked to. A malformed marker entry (wrong type, missing or
non-hash-shaped field) is rejected cleanly, never raises. `read_committed_run`
re-validates via `list_committed_runs()` itself rather than trusting the
caller to have done so first.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .run_validation import validate_run
from .store import ARCHIVE_DIRNAME, Tier1Store, sha256_file

BATCH_COMMIT_VERSION = "1.0.0"
SUPPORTED_BATCH_COMMIT_VERSIONS = frozenset({BATCH_COMMIT_VERSION})

_RUN_FILES = ("events.jsonl", "run.json", "manifest.json")


class ManagedEnvironmentStoreFilesystemError(Exception):
    """Staging and the real `runs/` tree are not on the same filesystem.

    `os.rename` across filesystems is not atomic — it silently degrades to
    copy-then-delete, reintroducing exactly the failure mode this module
    exists to close. There is deliberately no way to bypass this check.
    """


class ManagedEnvironmentBatchVerificationError(Exception):
    """A staged or committed run did not verify against what was intended.

    Covers: a staging read-back that doesn't match what was written, a
    staged archive whose hash no longer matches the manifest's declared
    value, or a post-rename re-validation (`run_validation.validate_run`)
    that fails against the path-corrected manifest.
    """


class ManagedEnvironmentRunIdCollision(Exception):
    """A run_id is already in use — committed or orphaned.

    Checking `<tier1_root>/runs/<run_id>` for bare existence catches both
    cases in one test: a committed run physically lives there too, and an
    orphan left by a prior interrupted commit occupies the same name
    without ever having been marker-validated.
    """


def _fsync_path(path: Path) -> None:
    """fsync a file or directory. Directories have no write-mode file handle
    in Python, so this uses the standard POSIX open/fsync/close technique,
    which works for both."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """`os.write` is not guaranteed to write every byte in one call — a
    short write is a real, documented possibility (this project has already
    been bitten by exactly this class of durability defect elsewhere), so
    every write here loops until every byte has actually landed."""
    view = memoryview(data)
    total = len(view)
    written = 0
    while written < total:
        n = os.write(fd, view[written:])
        if n <= 0:
            raise OSError(
                f"os.write made no progress ({n} bytes of {total - written} "
                f"remaining) — treating as a failed write rather than looping "
                f"forever"
            )
        written += n


def _atomic_write_json(path: Path, data: dict[str, Any], *, mode: int) -> None:
    """temp file (same dir, given mode) -> fsync -> atomic rename over `path`."""
    tmp = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        _write_all(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)


def _ensure_private_dir(path: Path) -> None:
    """Require exactly 0700 for `path` before any managed environment data is written
    into it — and require it to actually be a directory.

    round-7 correction: the original check only rejected group/other bits
    (`mode & 0o077`), which let a pre-existing `0600` or `0500` directory
    through even though neither is `0700`, and never checked that `path`
    was a directory at all rather than a regular file (or, via `os.lstat`
    here rather than `os.stat`, a symlink to one). A pre-existing path that
    fails either check is refused outright, not silently tightened or
    reinterpreted — silently changing the mode (or type) of something this
    module didn't create itself could have surprising side effects, and
    forcing the operator to fix it consciously matches this project's
    fail-closed posture elsewhere. A path that doesn't exist yet is created
    as a directory at exactly 0700 (the mode is forced explicitly after
    `mkdir`, since `mkdir`'s requested mode is filtered through the
    process's umask and can't be trusted alone).
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(parents=True)
        os.chmod(path, 0o700)
        return

    if not stat.S_ISDIR(st.st_mode):
        raise ManagedEnvironmentStoreFilesystemError(
            f"{path} already exists but is not a plain directory — refusing to "
            f"write managed environment content where a private directory is required "
            f"(a symlink is refused here too, since `os.lstat` reports the "
            f"link itself, not whatever it points to)"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o700:
        raise ManagedEnvironmentStoreFilesystemError(
            f"{path} already exists with mode {oct(mode)}, not exactly 0700 — "
            f"refusing to write managed environment content into a directory whose "
            f"permissions weren't set up as required. Fix its permissions to "
            f"0700 before retrying."
        )


def _is_uuid4_str(value: Any) -> bool:
    """True iff `value` is a UUIDv4 string in canonical lowercase form —
    exactly what `str(uuid.uuid4())` produces, which is how every batch_id
    and run_id in this module is generated."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _is_utc_timestamp_str(value: Any) -> bool:
    """True iff `value` parses as an ISO-8601 timestamp with a UTC offset —
    what `datetime.now(timezone.utc).isoformat()` produces, which is how
    every `committed_at` in this module is generated."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _make_removable(run_dir: Path) -> None:
    """Restore owner write+traverse on a hardened run directory so it can
    actually be deleted. `shutil.rmtree` cannot unlink a file whose
    containing directory lacks the write bit — exactly the mode a completed
    run's `raw/` and top-level directory deliberately have (`0500`) —
    so a rollback attempted after hardening must undo that first, or
    `ignore_errors=True` silently leaves the hardened contents behind
    rather than actually removing them."""
    if not run_dir.exists():
        return
    raw_dir = run_dir / ARCHIVE_DIRNAME
    if raw_dir.exists():
        os.chmod(raw_dir, 0o700)
    os.chmod(run_dir, 0o700)


@dataclass
class CommitOutcome:
    batch_id: str
    run_ids: list[str]
    marker_path: Path


class ManagedEnvironmentBatchStore:
    """Opt-in managed environment wrapper around `Tier1Store`. See module docstring."""

    def __init__(self, tier1_root: str | Path) -> None:
        self.root = Path(tier1_root).expanduser().resolve()
        # Reuses Tier1Store's outside-repo guarantee for the real root.
        Tier1Store(root=self.root)
        # round-6 correction: the root itself must be secured before
        # anything is written under it — a freshly created root previously
        # ended up 0755 (whatever mkdir(parents=True) + the process umask
        # produced), and only its children were explicitly chmod'd.
        _ensure_private_dir(self.root)
        self.runs_root = self.root / "runs"
        self.batches_dir = self.runs_root / "_batches"
        self.staging_base = self.root / "_staging"
        for d in (self.runs_root, self.batches_dir, self.staging_base):
            _ensure_private_dir(d)
        self._assert_same_filesystem()

    def _assert_same_filesystem(self) -> None:
        if os.stat(self.staging_base).st_dev != os.stat(self.runs_root).st_dev:
            raise ManagedEnvironmentStoreFilesystemError(
                f"{self.staging_base} and {self.runs_root} are not on the same "
                f"filesystem (st_dev differs). A managed environment deployment whose "
                f"staging and final roots can't share a filesystem needs a "
                f"different tier1_root choice, not a way to disable this check."
            )

    # -- run_id collision ---------------------------------------------------

    def run_id_in_use(self, run_id: str) -> bool:
        """True if `runs/<run_id>` exists at all — committed or orphaned.

        A committed run physically lives here too, so this one check covers
        both cases the design calls out separately; see
        `ManagedEnvironmentRunIdCollision`'s docstring.
        """
        return (self.runs_root / run_id).exists()

    # -- staging (Phase 4/5) -------------------------------------------------

    def staging_store(self, batch_id: str) -> Tier1Store:
        batch_root = self.staging_base / batch_id
        # round-6 correction: secure the batch's own staging root before
        # any archive or event write happens under it — everything nested
        # below inherits this directory's traversal protection, so this one
        # call is sufficient without also hardening every individual staged
        # run directory (which still needs to stay writable during staging
        # anyway).
        _ensure_private_dir(batch_root)
        return Tier1Store(root=batch_root)

    def archive(self, batch_id: str, run_id: str, snapshot: Any) -> tuple[Path, str]:
        """Phase 4 step 1: archive the captured snapshot into staging."""
        return self.staging_store(batch_id).archive_snapshot(run_id, snapshot)

    def stage(
        self, batch_id: str, *, events: list[dict[str, Any]], run: dict[str, Any], manifest: Any
    ) -> None:
        """Phase 5: write, then verify by read-back and archive re-hash."""
        store = self.staging_store(batch_id)
        run_id = run["run_id"]
        store.write_run(events=events, run=run, manifest=manifest)

        r_events, r_run, r_manifest = store.read_run(run_id)
        if r_events != events or r_run != run or r_manifest != manifest.to_dict():
            raise ManagedEnvironmentBatchVerificationError(
                f"run {run_id}: staged read-back does not match what was written "
                f"— possible disk-level write corruption"
            )

        arch_path = manifest.archived_transcript_path
        arch_sha = manifest.archived_transcript_sha256
        if arch_path and arch_sha:
            actual = sha256_file(arch_path)
            if actual != arch_sha:
                raise ManagedEnvironmentBatchVerificationError(
                    f"run {run_id}: staged archive at {arch_path} hashes "
                    f"{actual[:12]}… but the manifest declares {arch_sha[:12]}…"
                )

    def rollback_batch(self, batch_id: str) -> None:
        """Any failure in phases 2–5: delete the whole staging area for this
        batch. Nothing was ever renamed into `runs/`, so there is nothing
        else to undo."""
        shutil.rmtree(self.staging_base / batch_id, ignore_errors=True)

    # -- commit (Phase 6) -----------------------------------------------------

    def commit_batch(self, batch_id: str, run_ids: list[str]) -> CommitOutcome:
        """Phase 6, all nine steps. Raises and rolls back on any failure."""
        staging_root = self.staging_base / batch_id
        staging_store = self.staging_store(batch_id)

        # Steps 1-8 all happen inside one try/except: a failure at ANY point
        # after a run has been renamed -- not only the step-3 re-validation
        # called out explicitly in the design -- must roll back every run
        # already renamed for this batch. Leaving a later-stage failure
        # (e.g. a corrupted archive discovered only at marker-hash time)
        # un-rolled-back wouldn't expose anything invalid (no marker means
        # no visibility either way), but it would leave debris in `runs/`
        # that the explicit step-3 rollback path was specifically designed
        # to avoid -- there is no principled reason to treat a later failure
        # more permissively than an earlier one.
        renamed: list[str] = []
        try:
            for run_id in run_ids:
                self._rename_correct_and_revalidate(staging_store, run_id, renamed)

            # Steps 4-5: harden + fsync, per run.
            for run_id in run_ids:
                self._harden_and_fsync_run(run_id)

            # Step 6: fsync the shared runs/ directory once, after every
            # rename and hardening pass in the batch is done.
            _fsync_path(self.runs_root)

            # Step 7: compute marker hashes from final, hardened, fsynced files.
            runs_entries = [self._marker_entry_for(run_id) for run_id in run_ids]

            # Step 8: write the marker durably -- the actual commit point.
            marker_path = self._write_marker(batch_id, runs_entries)
        except Exception:
            for rid in renamed:
                # round-6 correction: a run already hardened (0500 dir,
                # 0400 files) by the time a LATER step fails cannot be
                # removed by rmtree as-is — unlinking a file requires write
                # permission on its *containing* directory, which hardening
                # deliberately revokes. Restore it first, or ignore_errors
                # silently leaves the hardened contents behind instead of
                # actually rolling them back.
                run_dir = self.runs_root / rid
                _make_removable(run_dir)
                shutil.rmtree(run_dir, ignore_errors=True)
            # A pathological failure between the marker's own rename and its
            # final fsync (step 8) could theoretically leave a marker file
            # on disk referencing run directories this except just deleted.
            # Not cleaned up here deliberately -- `_runs_from_marker_if_valid`
            # already rejects a marker whose referenced files are missing
            # (`except OSError: return []`), so such a marker is inert, not
            # a validity hole; over-engineering cleanup for an
            # already-covered edge case isn't worth the extra code path.
            raise

        # Step 9: staging is now empty of committed content; remove it.
        shutil.rmtree(staging_root, ignore_errors=True)

        return CommitOutcome(batch_id=batch_id, run_ids=list(run_ids), marker_path=marker_path)

    def _rename_correct_and_revalidate(
        self, staging_store: Tier1Store, run_id: str, renamed: list[str]
    ) -> None:
        src = staging_store.run_dir(run_id)
        dst = self.runs_root / run_id
        if dst.exists():
            raise ManagedEnvironmentRunIdCollision(
                f"run_id {run_id} already exists at {dst} — refusing to rename over it"
            )

        # Step 1: atomic rename.
        os.rename(src, dst)
        renamed.append(run_id)

        # Step 2: correct manifest.json's archived_transcript_path in place.
        # Truthful the instant it's written, because step 1 already moved
        # the archive to exactly that path.
        manifest_path = dst / "manifest.json"
        manifest_data = json.loads(manifest_path.read_text())
        old_archive_path = manifest_data.get("archived_transcript_path")
        if old_archive_path:
            filename = Path(old_archive_path).name
            final_archive_path = dst / ARCHIVE_DIRNAME / filename
            manifest_data["archived_transcript_path"] = str(final_archive_path)
            _atomic_write_json(manifest_path, manifest_data, mode=0o600)
        _fsync_path(dst)

        # Step 3: re-validate against the corrected manifest.
        events = [
            json.loads(line)
            for line in (dst / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        run = json.loads((dst / "run.json").read_text())
        rv = validate_run(events, run, manifest_data)
        if not rv.ok:
            raise ManagedEnvironmentBatchVerificationError(
                f"run {run_id}: post-rename re-validation failed — {rv.summary()}"
            )

    def _harden_and_fsync_run(self, run_id: str) -> None:
        run_dir = self.runs_root / run_id
        raw_dir = run_dir / ARCHIVE_DIRNAME

        for name in _RUN_FILES:
            os.chmod(run_dir / name, 0o400)
        for p in raw_dir.iterdir():
            os.chmod(p, 0o400)
        os.chmod(raw_dir, 0o500)
        os.chmod(run_dir, 0o500)

        for name in _RUN_FILES:
            _fsync_path(run_dir / name)
        for p in raw_dir.iterdir():
            _fsync_path(p)
        _fsync_path(raw_dir)
        _fsync_path(run_dir)

    def _marker_entry_for(self, run_id: str) -> dict[str, Any]:
        run_dir = self.runs_root / run_id
        raw_dir = run_dir / ARCHIVE_DIRNAME
        archive_files = list(raw_dir.iterdir())
        if len(archive_files) != 1:
            raise ManagedEnvironmentBatchVerificationError(
                f"run {run_id}: archive directory {raw_dir} has "
                f"{len(archive_files)} files, expected exactly 1"
            )
        return {
            "run_id": run_id,
            "events_sha256": sha256_file(run_dir / "events.jsonl"),
            "run_sha256": sha256_file(run_dir / "run.json"),
            "manifest_sha256": sha256_file(run_dir / "manifest.json"),
            "archived_transcript_sha256": sha256_file(archive_files[0]),
        }

    def _write_marker(self, batch_id: str, runs_entries: list[dict[str, Any]]) -> Path:
        run_ids = [r["run_id"] for r in runs_entries]
        if len(run_ids) != len(set(run_ids)):
            raise ManagedEnvironmentBatchVerificationError(
                f"batch {batch_id}: duplicate run_id in marker entries — refusing "
                f"to write a marker that can't distinguish its own runs"
            )
        marker = {
            "batch_commit_version": BATCH_COMMIT_VERSION,
            "batch_id": batch_id,
            "committed_at": datetime.now(timezone.utc).isoformat(),
            "runs": runs_entries,
        }
        marker_path = self.batches_dir / f"{batch_id}.json"
        _atomic_write_json(marker_path, marker, mode=0o600)
        os.chmod(marker_path, 0o400)
        _fsync_path(marker_path)
        _fsync_path(self.batches_dir)  # the actual commit point
        return marker_path

    # -- reads (the only managed environment read path) --------------------------------

    def list_committed_runs(self) -> list[str]:
        """The only trustworthy source of "what managed environment runs are valid."

        Never `Tier1Store.list_runs()` — a bare directory listing includes
        orphans with no marker validation at all.
        """
        valid: set[str] = set()
        for marker_path in sorted(self.batches_dir.glob("*.json")):
            run_ids = self._runs_from_marker_if_valid(marker_path)
            valid.update(run_ids)
        return sorted(valid)

    def _runs_from_marker_if_valid(self, marker_path: Path) -> list[str]:
        try:
            marker = json.loads(marker_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(marker, dict):
            return []
        for key in ("batch_commit_version", "batch_id", "committed_at", "runs"):
            if key not in marker:
                return []
        # Fail-closed on an unrecognized batch_commit_version, same posture
        # as versions.py's version_failure elsewhere in this project — kept
        # as a local, direct check rather than registered there, since this
        # marker format is internal to this module's own reader, not a
        # cross-module document contract like manifest_version is.
        if marker["batch_commit_version"] not in SUPPORTED_BATCH_COMMIT_VERSIONS:
            return []
        # round-7 addition: validate the rest of the marker's own contract,
        # not only its run entries — a marker whose batch_id isn't actually
        # a UUIDv4 or whose committed_at isn't a parseable UTC timestamp is
        # just as malformed as one with a bad run entry, and must reject
        # the whole marker the same way.
        if not _is_uuid4_str(marker["batch_id"]):
            return []
        if not _is_utc_timestamp_str(marker["committed_at"]):
            return []
        runs = marker["runs"]
        if not isinstance(runs, list) or not runs:
            return []

        # round-6 correction: validate every entry's shape BEFORE touching
        # it with .get()/subscript — a malformed marker whose `runs` list
        # contains a non-object (or an object missing a hash field, or a
        # non-hex-shaped hash) must be cleanly rejected, never raise. Every
        # hash field is required to look like an actual sha256 hex digest
        # (64 lowercase hex characters) — "hash-shaped," not merely present.
        hash_keys = (
            "events_sha256", "run_sha256", "manifest_sha256", "archived_transcript_sha256",
        )
        for entry in runs:
            if not isinstance(entry, dict):
                return []
            if not isinstance(entry.get("run_id"), str) or not entry["run_id"]:
                return []
            for key in hash_keys:
                value = entry.get(key)
                if not isinstance(value, str) or len(value) != 64:
                    return []
                # round-7 correction: checking `value.lower()` made this
                # accidentally case-insensitive — uppercase hex passed even
                # though nothing in this project ever emits it (hexdigest()
                # is always lowercase). Require the value to already BE
                # lowercase, and check its own characters, not a lowered copy.
                if value != value.lower():
                    return []
                if not all(c in "0123456789abcdef" for c in value):
                    return []

        run_ids = [entry["run_id"] for entry in runs]
        if len(run_ids) != len(set(run_ids)):
            return []

        for entry in runs:
            run_id = entry["run_id"]
            run_dir = self.runs_root / run_id
            raw_dir = run_dir / ARCHIVE_DIRNAME
            try:
                for name, key in (
                    ("events.jsonl", "events_sha256"),
                    ("run.json", "run_sha256"),
                    ("manifest.json", "manifest_sha256"),
                ):
                    if sha256_file(run_dir / name) != entry.get(key):
                        return []
                archive_files = list(raw_dir.iterdir())
                if len(archive_files) != 1:
                    return []
                if sha256_file(archive_files[0]) != entry.get("archived_transcript_sha256"):
                    return []
            except OSError:
                return []
        return [r["run_id"] for r in runs]

    def read_committed_run(
        self, run_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Read a run, refusing unless it is itself currently a committed,
        marker-valid run.

        round-6 correction: this used to trust the caller to have called
        `list_committed_runs()` first — a bypassable managed environment read path,
        since nothing stopped a caller from passing an orphaned or
        marker-invalid run_id directly. It now performs that check itself,
        every time, rather than documenting it as someone else's
        responsibility.
        """
        if run_id not in self.list_committed_runs():
            raise ManagedEnvironmentBatchVerificationError(
                f"run_id {run_id} is not a committed run — no valid batch-commit "
                f"marker references it — refusing to read it as if it were"
            )
        run_dir = self.runs_root / run_id
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        run = json.loads((run_dir / "run.json").read_text())
        manifest = json.loads((run_dir / "manifest.json").read_text())
        return events, run, manifest
