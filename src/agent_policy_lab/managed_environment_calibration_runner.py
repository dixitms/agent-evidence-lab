"""Managed-environment-local calibration runner (provisional, fixture-only).

the managed-environment calibration runner design, settled after five review
rounds. Orchestrates `adapter_managed_environment_claude_code.normalize_managed_environment_session`
over an explicit, fixed **intake manifest** (never a directory scan, never
automatic session discovery), staging and committing every result through
`managed_environment_batch_store.ManagedEnvironmentBatchStore` so a batch is genuinely
all-or-nothing.

No real managed environment trace has ever been read by this module or by anything
that produced it — every fixture exercising it is hand-invented, same
discipline as `fixtures/raw/README.md`. This module implements **no
detector, dashboard, export path, organisation report, directory scan, or
individual/team metric** — it is capture → normalize → reconcile → validate
→ stage → commit, and nothing else. Every batch is hardcoded
`trace_profile="managed-environment-live-provisional"`; the adapter itself already
refuses anything else (`TraceProfileUnavailable`), and this module never
exposes a way to request one.

Six phases:

1. Capture every manifest file's bytes+hash before any parsing.
2. Assign a fresh random `run_id` (collision against this batch or an
   existing `runs/<run_id>` — committed or orphaned — refuses the whole
   batch, never silently regenerated), then normalize from the captured
   bytes only.
3. Reconcile against the **live** path, not the cached snapshot — the one
   call that catches both snapshot drift and per-class reconciliation
   failure.
4. Archive the captured snapshot into staging *first*, then build the
   manifest with that archive's real path/hash, then validate via the
   unchanged `run_validation.validate_run`.
5. Stage (write + read-back verify).
6. Commit the whole batch atomically, or roll back everything if any file
   failed anywhere in phases 2-5.
"""

from __future__ import annotations

import json
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .adapter_managed_environment_claude_code import normalize_managed_environment_session, reconcile
from .managed_environment_batch_store import ManagedEnvironmentBatchStore, ManagedEnvironmentRunIdCollision
from .run_validation import validate_run
from .snapshot import SessionSnapshot
from .store import build_manifest

TRACE_PROFILE = "managed-environment-live-provisional"

INTAKE_MANIFEST_FORMAT_VERSION = "1.0.0"
SUPPORTED_INTAKE_MANIFEST_VERSIONS = frozenset({INTAKE_MANIFEST_FORMAT_VERSION})


class IntakeManifestError(Exception):
    """The intake manifest itself is malformed, insecure, or out of scope.

    Refuses before a single byte of any listed file is read — an intake
    manifest is a pre-authorised, fixed list, never a directory to walk or
    a pattern to expand.
    """


class CalibrationBatchError(Exception):
    """A file failed some gate during phases 2-5. The whole batch refuses."""


def load_intake_manifest(path: str | Path) -> list[Path]:
    """Load and validate the fixed intake manifest.

    A versioned local JSON
    object naming absolute file paths only — never a directory, glob, or
    query. Refuses: an unrecognized `manifest_format_version`; a non-`0600`
    file mode (group/other readable or writable); a non-absolute path
    entry; an entry resolving to a directory; a duplicate path.
    """
    p = Path(path)
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise IntakeManifestError(
            f"{p}: mode {oct(mode)} is not private enough — the intake manifest "
            f"must be 0600 (no group/other bits), since it lists real managed environment "
            f"file paths"
        )

    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise IntakeManifestError(f"{p}: must be a JSON object, not {type(data).__name__}")

    version = data.get("manifest_format_version")
    if version not in SUPPORTED_INTAKE_MANIFEST_VERSIONS:
        raise IntakeManifestError(
            f"{p}: unrecognized manifest_format_version {version!r} — refusing "
            f"to read it with assumptions from another version "
            f"(supported: {sorted(SUPPORTED_INTAKE_MANIFEST_VERSIONS)})"
        )

    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise IntakeManifestError(f"{p}: 'files' must be a non-empty list")

    seen: set[str] = set()
    result: list[Path] = []
    for entry in files:
        if not isinstance(entry, str) or not entry.startswith("/"):
            raise IntakeManifestError(
                f"{p}: {entry!r} is not an absolute path string — no wildcard, "
                f"no relative path, no directory"
            )
        if entry in seen:
            raise IntakeManifestError(f"{p}: duplicate path in intake manifest: {entry}")
        seen.add(entry)
        entry_path = Path(entry)
        if entry_path.exists() and entry_path.is_dir():
            raise IntakeManifestError(
                f"{p}: {entry} resolves to a directory, not a file — the intake "
                f"manifest names files only, never something to walk"
            )
        result.append(entry_path)
    return result


@dataclass
class BatchOutcome:
    committed: bool
    batch_id: str
    run_ids: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    marker_path: Path | None = None


def run_calibration_batch(
    intake_manifest_path: str | Path,
    *,
    tier1_root: str | Path,
    local_secret: bytes,
    repo_ref: str,
    tool_name_allowlist: frozenset[str] = frozenset(),
    trigger_label_allowlist: frozenset[str] = frozenset(),
) -> BatchOutcome:
    """Run one calibration batch, per the intake manifest, atomically.

    `trace_profile` is not a parameter — this runner only ever requests
    `"managed-environment-live-provisional"`; the adapter already refuses anything
    else, and this function never exposes a way to ask for the archived
    cohort.
    """
    files = load_intake_manifest(intake_manifest_path)
    batch_store = ManagedEnvironmentBatchStore(tier1_root)
    batch_id = str(uuid.uuid4())

    run_ids: list[str] = []
    try:
        # Phase 1: capture every file's bytes+hash before any parsing.
        snapshots: dict[Path, SessionSnapshot] = {f: SessionSnapshot.capture(f) for f in files}

        assigned: set[str] = set()
        for f in files:
            snapshot = snapshots[f]

            # Phase 2: assign run_id (collision refuses, never regenerates), normalize.
            run_id = str(uuid.uuid4())
            if run_id in assigned or batch_store.run_id_in_use(run_id):
                raise ManagedEnvironmentRunIdCollision(
                    f"generated run_id {run_id} for {f} already in use"
                )
            assigned.add(run_id)

            result = normalize_managed_environment_session(
                snapshot,
                run_id=run_id,
                local_secret=local_secret,
                repo_ref=repo_ref,
                trace_profile=TRACE_PROFILE,
                tool_name_allowlist=tool_name_allowlist,
                trigger_label_allowlist=trigger_label_allowlist,
            )

            # Phase 3: reconcile against the LIVE path — catches drift and
            # per-class reconciliation failure in one call.
            reconciliation = reconcile(f, result)

            # Phase 4: archive into staging first, then build+validate the manifest.
            archived_path, archived_sha256 = batch_store.archive(batch_id, run_id, snapshot)
            manifest = build_manifest(
                result=result,
                reconciliation=reconciliation,
                raw_local_path=f,
                trace_profile=TRACE_PROFILE,
                archived_transcript_path=str(archived_path),
                archived_transcript_sha256=archived_sha256,
            )
            if manifest.trace_profile != TRACE_PROFILE:
                raise CalibrationBatchError(
                    f"{f}: manifest.trace_profile={manifest.trace_profile!r}, "
                    f"expected {TRACE_PROFILE!r}"
                )
            if manifest.archived_transcript_sha256 != result.snapshot_sha256:
                raise CalibrationBatchError(
                    f"{f}: manifest.archived_transcript_sha256 does not match "
                    f"result.snapshot_sha256"
                )
            rv = validate_run(result.events, result.run, manifest.to_dict())
            if not rv.ok:
                raise CalibrationBatchError(f"{f}: {rv.summary()}")

            # Phase 5: stage (write + read-back verify).
            batch_store.stage(batch_id, events=result.events, run=result.run, manifest=manifest)
            run_ids.append(run_id)

    except Exception as exc:
        batch_store.rollback_batch(batch_id)
        return BatchOutcome(
            committed=False, batch_id=batch_id, run_ids=[], failure_reason=str(exc)
        )

    # Phase 6: commit the whole batch atomically, or refuse it whole.
    try:
        outcome = batch_store.commit_batch(batch_id, run_ids)
    except Exception as exc:
        return BatchOutcome(
            committed=False, batch_id=batch_id, run_ids=[], failure_reason=str(exc)
        )

    return BatchOutcome(
        committed=True,
        batch_id=batch_id,
        run_ids=outcome.run_ids,
        marker_path=outcome.marker_path,
    )
