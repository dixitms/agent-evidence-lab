"""Local discovery diagnostic runner: orchestration only.

Accepts an explicit, local-only session manifest; snapshots every session
before any processing begins; normalizes and reconciles each one, aborting
the entire run if any session fails reconciliation; computes exactly the six
approved diagnostics per `DIAGNOSTICS_SPEC`; assembles the report; and writes
it through `local_discovery_report.create_report` -- the only function in
this codebase permitted to write a local discovery report.

This module does not read a session, reconcile one, or write a report by any
means of its own -- `SessionSnapshot`, `adapter_claude_code.normalize_session`/
`reconcile`, and `local_discovery_report.create_report` are reused, not
reimplemented. No CLI, no auto-discovery, no default manifest: every session
in scope for a run is named explicitly by the caller in that one call. No
real session is read, selected, or scanned by anything in this module by
default -- a caller must supply real paths for that to happen, and this
module never invents its own.

`adapter_claude_code` is imported as a module (not `from .adapter_claude_code
import normalize_session`) specifically so a test can monkeypatch
`adapter_claude_code.reconcile` to force a deterministic reconciliation
failure without needing a real malformed session file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import adapter_claude_code
from . import local_discovery_report
from .local_discovery_diagnostics import DIAGNOSTICS_SPEC, SessionDiagnosticInput
from .snapshot import SessionSnapshot

# This file is src/agent_evidence_lab/local_discovery_runner.py, so parents[2] is
# the repo root -- mirrors local_discovery_report.py's own boundary check,
# applied here to the *input* side (source_path) as well as the output side.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class LocalDiscoveryManifestInvalid(Exception):
    """The session manifest failed one of its explicit pre-snapshot checks
    (non-empty, unique session_id, unique resolved source_path, existing
    .jsonl regular file, session_id matching the file's own stem, or a
    source_path outside the repository)."""


class LocalDiscoveryRunAborted(Exception):
    """At least one session in this run failed reconciliation. The entire
    run is refused -- no diagnostic is computed for any session, and no
    report is written -- per the local-discovery report contract's
    fail-fast rule: "Snapshot or reconciliation mismatch -> reject the
    run/report; never emit a partial 'mostly reconciled' diagnostic." """


@dataclass(frozen=True)
class SessionManifestEntry:
    session_id: str
    source_path: Path


def _assert_outside_repo(path: Path) -> None:
    try:
        path.relative_to(_REPO_ROOT)
    except ValueError:
        return
    raise LocalDiscoveryManifestInvalid(
        f"{path} is inside the repository at {_REPO_ROOT} -- session sources "
        "must live outside the working tree, mirroring local_discovery_report.py's "
        "Tier 1 boundary applied here to the input side"
    )


def validate_manifest(manifest: list[SessionManifestEntry]) -> None:
    """Seven explicit checks, in order, before any snapshotting.

    Raises on the first violation found -- see LocalDiscoveryManifestInvalid.
    """
    if not manifest:
        raise LocalDiscoveryManifestInvalid("manifest must not be empty")

    session_ids = [e.session_id for e in manifest]
    if len(session_ids) != len(set(session_ids)):
        dupes = sorted({s for s in session_ids if session_ids.count(s) > 1})
        raise LocalDiscoveryManifestInvalid(f"duplicate session_id(s): {dupes}")

    resolved = [e.source_path.resolve() for e in manifest]
    if len(resolved) != len(set(resolved)):
        dupes = sorted({str(p) for p in resolved if resolved.count(p) > 1})
        raise LocalDiscoveryManifestInvalid(f"duplicate resolved source_path(s): {dupes}")

    for entry, path in zip(manifest, resolved):
        if not path.is_file():
            raise LocalDiscoveryManifestInvalid(
                f"{path} does not exist or is not a regular file"
            )
        if path.suffix != ".jsonl":
            raise LocalDiscoveryManifestInvalid(
                f"{path} does not have a .jsonl suffix -- this is a Claude "
                "Code-only runner and refuses a wrong regular file before "
                "snapshotting it"
            )
        if path.stem != entry.session_id:
            raise LocalDiscoveryManifestInvalid(
                f"session_id {entry.session_id!r} does not match the filename "
                f"stem {path.stem!r} of {path} -- this Claude Code-only pass "
                "requires them to agree"
            )
        _assert_outside_repo(path)


def _require_all_reconciled(reconciliations: dict[str, dict]) -> None:
    """Pure -- no I/O, no adapter call. Raises on the first session/class that
    isn't ok. Its own function specifically so a test can call it directly
    with a hand-built fake reconciliation dict, proving abort behavior
    without needing to guess which malformed JSONL would make the real
    adapter disagree with itself."""
    for session_id, report in reconciliations.items():
        if not all(entry.get("ok") for entry in report.values()):
            raise LocalDiscoveryRunAborted(
                f"session {session_id} failed reconciliation: {report}"
            )


def run(
    manifest: list[SessionManifestEntry],
    *,
    report_id: str,
    root: str | Path = local_discovery_report.DEFAULT_ROOT,
    label: str | None = None,
) -> Path:
    """Validate the manifest, snapshot every session, normalize and reconcile
    each one (aborting the whole run on any mismatch), compute exactly the
    six approved diagnostics, assemble the report, and write it through
    `local_discovery_report.create_report`.

    Nothing here authorizes a real run on its own -- the caller supplies real
    paths only when a real run has been separately, explicitly authorized.
    """
    validate_manifest(manifest)

    snapshots = {entry.session_id: SessionSnapshot.capture(entry.source_path) for entry in manifest}

    normalized: dict[str, Any] = {}
    reconciliations: dict[str, dict] = {}
    for entry in manifest:
        snapshot = snapshots[entry.session_id]
        result = adapter_claude_code.normalize_session(
            snapshot,
            run_id=f"local-discovery-{snapshot.sha256[:16]}",
            repo_root=None,
            local_secret=b"local-discovery-runner",
            repo_ref=f"sha256:{snapshot.sha256}",
            run_kind="private_dogfood",
            task_id="local-discovery-diagnostic",
        )
        normalized[entry.session_id] = result
        reconciliations[entry.session_id] = adapter_claude_code.reconcile(snapshot, result)

    _require_all_reconciled(reconciliations)

    per_diagnostic_per_session: dict[str, dict[str, dict]] = {d.name: {} for d in DIAGNOSTICS_SPEC}
    for entry in manifest:
        ctx = SessionDiagnosticInput(
            events=normalized[entry.session_id].events,
            reconciliation=reconciliations[entry.session_id],
        )
        for d in DIAGNOSTICS_SPEC:
            per_diagnostic_per_session[d.name][entry.session_id] = d.calculate(ctx)

    results: dict[str, dict] = {}
    availability: dict[str, dict] = {}
    considered = len(manifest)
    for d in DIAGNOSTICS_SPEC:
        per_session_leaves = per_diagnostic_per_session[d.name]
        results[d.name] = {
            "per_session": per_session_leaves,
            "aggregate": d.aggregate(per_session_leaves),
        }
        computed = sum(1 for leaf in per_session_leaves.values() if leaf["outcome"] == "computed")
        not_reportable = sum(
            1 for leaf in per_session_leaves.values() if leaf["outcome"] == "not_reportable"
        )
        excluded = considered - computed - not_reportable
        availability[d.name] = {
            "sessions": {
                "considered": considered,
                "computed": computed,
                "not_reportable": not_reportable,
                "excluded": excluded,
            }
        }

    diagnostics_field = [
        {
            "name": d.name,
            "measure_kind": d.measure_kind,
            "rule_version": d.rule_version,
            "signal_vocabulary": list(d.signal_vocabulary),
        }
        for d in DIAGNOSTICS_SPEC
    ]

    input_sessions = [
        {
            "session_id": entry.session_id,
            "snapshot_sha256": f"sha256:{snapshots[entry.session_id].sha256}",
        }
        for entry in manifest
    ]

    scope = {
        "subject_count": 1,
        "repository_count": 1,
        "session_count": considered,
        "source_client": "claude-code",
    }
    if label is not None:
        scope["label"] = label

    resolved_root = Path(root).expanduser().resolve()
    report = {
        "report_id": report_id,
        "report_path": str(resolved_root / f"{report_id}.json"),
        "report_version": local_discovery_report.REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_sessions": input_sessions,
        "diagnostics": diagnostics_field,
        "scope": scope,
        "availability": availability,
        "results": results,
        "public_evidence": False,
        "organisational_claim_licensed": False,
    }

    return local_discovery_report.create_report(report, root=root)
