"""Whole-run validation.

One gate over a stored run, so a study cannot produce data nobody checked.
Four independent things must hold, and they are genuinely different questions:

1. **Schema** — every event and the run record validate against the contract
   version they *declare*. Versions are never pooled: a 0.1.1 event is checked
   against `schema/v0.1`, a 0.2.0 event against `schema/v0.2`.
2. **Dataset** — the cross-record checks JSON Schema structurally cannot make
   (`validators.py`): duplicate `sequence_index`, dangling `run_id`,
   `field_status` coverage, assistant-envelope accounting.
3. **Reconciliation** — per class against the authoritative raw class. Passing
   1 and 2 while missing most of the corpus is exactly what happened once.
4. **Manifest agreement** — the manifest's own counts and hashes match what is
   stored beside it. A manifest that miscounts its run is not provenance.

A run is `validated` only if all four hold. There is no partial pass.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .store import Tier1Store
from .validators import validate_dataset
from .versions import version_failure


def _schema_root() -> Path:
    """Find the shipped schemas in a checkout or installed wheel."""
    candidates = (
        Path(__file__).resolve().parents[2] / "schema",
        Path(sys.prefix) / "schema",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Agent Policy Lab schemas are not installed")

# Event contract version -> directory. Adding a version means adding a row,
# not widening a glob: an unknown version must fail, not be guessed at.
SCHEMA_DIRS = {"0.1.1": "v0.1", "0.2.0": "v0.2"}


class UnknownSchemaVersion(Exception):
    """A record declares a contract version this validator does not know."""


@dataclass
class RunValidation:
    run_id: str
    schema_errors: list[str] = field(default_factory=list)
    dataset_findings: list[str] = field(default_factory=list)
    reconciliation_failures: list[str] = field(default_factory=list)
    manifest_disagreements: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.schema_errors
            or self.dataset_findings
            or self.reconciliation_failures
            or self.manifest_disagreements
        )

    def summary(self) -> str:
        if self.ok:
            return f"{self.run_id}: validated"
        parts = [
            f"{len(self.schema_errors)} schema",
            f"{len(self.dataset_findings)} dataset",
            f"{len(self.reconciliation_failures)} reconciliation",
            f"{len(self.manifest_disagreements)} manifest",
        ]
        return f"{self.run_id}: FAILED — " + ", ".join(parts)


def _validator_for(version: str, kind: str) -> Draft202012Validator:
    if version not in SCHEMA_DIRS:
        raise UnknownSchemaVersion(
            f"no schema directory registered for version {version!r}. Add it to "
            f"SCHEMA_DIRS deliberately — an unrecognized contract version must not "
            f"be validated against a guessed one."
        )
    path = _schema_root() / SCHEMA_DIRS[version] / f"{kind}.schema.json"
    return Draft202012Validator(json.loads(path.read_text()))


def validate_run(
    events: list[dict[str, Any]],
    run: dict[str, Any],
    manifest: dict[str, Any],
) -> RunValidation:
    rv = RunValidation(run_id=run.get("run_id", "<unknown>"))

    # 1. schema, per declared version
    for ev in events:
        v = _validator_for(ev.get("schema_version", ""), "agent_event")
        for err in sorted(v.iter_errors(ev), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "<root>"
            rv.schema_errors.append(
                f"event {ev.get('event_id', '?')} ({ev.get('event_type')}) {loc}: {err.message}"
            )
    rvv = _validator_for(run.get("schema_version", ""), "experiment_run")
    for err in sorted(rvv.iter_errors(run), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        rv.schema_errors.append(f"run record {loc}: {err.message}")

    # events must not straddle contract versions within one run
    versions = {ev.get("schema_version") for ev in events}
    if len(versions) > 1:
        rv.schema_errors.append(
            f"run mixes contract versions {sorted(versions)} — versions are never pooled"
        )
    if versions and run.get("schema_version") not in versions:
        rv.schema_errors.append(
            f"run record declares {run.get('schema_version')} but its events declare "
            f"{sorted(versions)}"
        )

    # 2. dataset-level checks
    rv.dataset_findings = list(validate_dataset(events, [run]).findings)

    # 3. reconciliation, as recorded by the adapter at ingestion time
    for cls, entry in (manifest.get("reconciliation") or {}).items():
        if not entry.get("ok"):
            rv.reconciliation_failures.append(f"{cls}: {entry}")
    if not manifest.get("reconciliation"):
        rv.reconciliation_failures.append(
            "manifest carries no reconciliation report — an unreconciled run is "
            "not validated data"
        )

    # 4. manifest agreement — starting with the format contract itself
    mv = version_failure("manifest_version", manifest.get("manifest_version"))
    if mv:
        rv.manifest_disagreements.append(mv)
    if manifest.get("event_count") != len(events):
        rv.manifest_disagreements.append(
            f"manifest event_count={manifest.get('event_count')} but {len(events)} stored"
        )
    if manifest.get("run_id") != run.get("run_id"):
        rv.manifest_disagreements.append("manifest run_id does not match run record")
    if manifest.get("schema_version") != run.get("schema_version"):
        rv.manifest_disagreements.append("manifest schema_version does not match run record")
    for key in ("pinned_commit", "run_kind", "condition", "disposition"):
        if manifest.get(key) != run.get(key):
            rv.manifest_disagreements.append(
                f"manifest {key}={manifest.get(key)!r} but run record has {run.get(key)!r}"
            )
    # The archived transcript is what makes a run re-verifiable later. A
    # manifest that declares one must still have it, with the same bytes.
    arch_path, arch_sha = (manifest.get("archived_transcript_path"),
                           manifest.get("archived_transcript_sha256"))
    if arch_path or arch_sha:
        if not (arch_path and arch_sha):
            rv.manifest_disagreements.append(
                "archived transcript is half-declared — path and hash must both be present")
        else:
            p = Path(arch_path)
            if not p.exists():
                rv.manifest_disagreements.append(
                    f"archived transcript missing at {arch_path} — the run can no "
                    f"longer be re-verified from its raw source")
            else:
                import hashlib as _h
                actual = _h.sha256(p.read_bytes()).hexdigest()
                if actual != arch_sha:
                    rv.manifest_disagreements.append(
                        f"archived transcript has changed since ingestion "
                        f"({actual[:12]}… vs declared {arch_sha[:12]}…)")

    if manifest.get("reconciled_ok") is not True and not rv.reconciliation_failures:
        rv.manifest_disagreements.append(
            "manifest.reconciled_ok is not true while every class reports ok — "
            "the summary flag disagrees with its own detail"
        )
    return rv


def validate_stored_run(store: Tier1Store, run_id: str) -> RunValidation:
    events, run, manifest = store.read_run(run_id)
    return validate_run(events, run, manifest)


def validate_all(store: Tier1Store) -> dict[str, RunValidation]:
    return {rid: validate_stored_run(store, rid) for rid in store.list_runs()}
