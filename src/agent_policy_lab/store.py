"""Tier 1 local store and run manifests.

Tier 1 is the local raw store: normalized events with
`raw_source_ref` intact, plus the run record and a provenance manifest. It may
contain real prompts, real code, real tool output. It lives outside the
repository and never enters git — enforced here, not merely intended: writing
the store inside the working tree is refused outright.

Nothing in this module exports. The fail-closed public export validator is
separate, and an export path that skipped it would defeat the whole boundary.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .snapshot import SessionSnapshot

# 1.1.0 added delivered_prompt_sha256 (D5) and environment_fingerprint (D6).
# 1.2.0 adds the archived transcript. 1.3.0 adds the final-state attestation.
# 1.4.0 adds trace_profile --
# which trace profile produced a run, e.g. "managed-environment-live-provisional".
# All minor: optional additions, no breaks.
MANIFEST_VERSION = "1.4.0"
ARCHIVE_DIRNAME = "raw"
DEFAULT_ROOT = Path.home() / ".agent-policy-lab" / "raw"

# This file is src/agent_policy_lab/store.py, so parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Tier1PathError(Exception):
    """Refused to place the Tier 1 store inside the repository working tree.

    "Gitignored" is not the control. A store inside
    the tree is one `git add -f`, one ignore-rule edit, or one careless glob
    away from committing real prompts and tool output.
    """


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class RunManifest:
    """Provenance for one ingested session: what was measured, not what was said.

    Deliberately hash-and-identity only — no prompt text, no tool output, no
    message content. A manifest records what was measured so a run can be
    re-checked later without retaining what was said.
    """

    run_id: str
    source_client: str
    source_client_version: str | None
    schema_version: str            # the event contract these events declare
    event_count: int
    session_snapshot_sha256: str   # the exact bytes normalized
    raw_local_path: str            # Tier 1 only; stripped from any export

    task_id: str
    pinned_commit: str
    repo_url: str

    policy_id: str | None
    policy_version: str | None
    policy_text_sha256: str | None   # hash of the intervention text, not the text
    task_prompt_sha256: str | None   # hash of the frozen prompt, not the prompt

    model: str | None
    model_source: str | None
    model_evidence_consistent: bool | None

    run_kind: str
    condition: str
    disposition: str
    disposition_reason: str | None

    disposition_counts: dict[str, int]
    preserved_detail: dict[str, int]
    reconciliation: dict[str, Any]
    reconciled_ok: bool

    # D5: hash of the prompt the agent actually received, verified against the
    # frozen file. A hash, never the text.
    delivered_prompt_sha256: str | None = None
    # D6: what the session was offered and what it used. Availability is
    # controlled by consistency; usage is a visible exclusion.
    environment_fingerprint: dict[str, Any] | None = None

    # The transcript, copied into Tier 1. `raw_local_path` records where it came
    # from; this records where it will still be. The client owns
    # ~/.claude/projects and may rotate or compact it, so provenance that points
    # only there is provenance with a expiry date nobody controls.
    archived_transcript_path: str | None = None
    archived_transcript_sha256: str | None = None

    # What the session left behind. `archived_transcript_*` pins what was said;
    # this pins what was produced, so a re-measurement can prove it measured this
    # run rather than a workspace that drifted since.
    final_state_attestation: dict[str, Any] | None = None

    # 1.4.0: which trace profile produced this run, e.g.
    # "managed-environment-live-provisional". None for every caller that doesn't pass
    # one -- in particular every existing personal-adapter caller, whose
    # behavior is unchanged by this field's addition. A caller that DOES
    # care (the managed environment calibration runner) is expected to check this
    # value itself; the field's mere presence enforces nothing on its own.
    trace_profile: str | None = None

    manifest_version: str = MANIFEST_VERSION
    capture_date: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Tier1Store:
    """Filesystem layout: <root>/runs/<run_id>/{events.jsonl,run.json,manifest.json}."""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root).expanduser().resolve()
        self._assert_outside_repo()

    def _assert_outside_repo(self) -> None:
        try:
            self.root.relative_to(_REPO_ROOT)
        except ValueError:
            return  # outside the tree, which is the requirement
        raise Tier1PathError(
            f"Tier 1 store root {self.root} is inside the repository at {_REPO_ROOT}. "
            f"Raw capture must live outside the working tree — gitignoring it is not "
            f"the control."
        )

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def write_run(
        self,
        *,
        events: list[dict[str, Any]],
        run: dict[str, Any],
        manifest: RunManifest,
    ) -> Path:
        if manifest.event_count != len(events):
            raise ValueError(
                f"manifest declares {manifest.event_count} events but {len(events)} "
                f"were given — a manifest that miscounts its own run is not provenance"
            )
        if manifest.run_id != run["run_id"]:
            raise ValueError("manifest run_id does not match the run record")

        d = self.run_dir(manifest.run_id)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "events.jsonl").open("w") as fh:
            for ev in events:
                fh.write(json.dumps(ev, sort_keys=True) + "\n")
        (d / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
        (d / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
        )
        return d

    def archive_transcript(self, run_id: str, source: str | Path) -> tuple[Path, str]:
        """Copy the raw transcript into Tier 1 and make it read-only.

        Returns (archived_path, sha256). The copy is chmod 0444 — not a security
        control, but enough that an overwrite has to be deliberate rather than a
        stray redirect. Re-archiving the same bytes is idempotent; re-archiving
        *different* bytes under the same run raises, because a run's raw source
        is not something that changes after the fact.
        """
        src = Path(source)
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        dest_dir = self.run_dir(run_id) / ARCHIVE_DIRNAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name

        if dest.exists():
            existing = hashlib.sha256(dest.read_bytes()).hexdigest()
            if existing != digest:
                raise ValueError(
                    f"{dest} already archived with a different hash "
                    f"({existing[:12]}… vs {digest[:12]}…) — a run's raw source does "
                    f"not change after the fact"
                )
            return dest, digest

        dest.write_bytes(src.read_bytes())
        dest.chmod(0o444)
        return dest, digest

    def archive_snapshot(self, run_id: str, snapshot: SessionSnapshot) -> tuple[Path, str]:
        """Archive already-captured bytes — never a fresh disk read.

        Additive alongside `archive_transcript`, not a replacement: that
        method still re-reads `source` live, which is fine for a caller that
        only has a path. This one exists for a caller that already captured
        a `SessionSnapshot` earlier (e.g. before reconciling against the live
        path) and wants the archive to be exactly those bytes, with no
        second read of the original — closing the small window a second live
        read would otherwise leave between "confirmed it matched" and
        "copied it".

        Returns (archived_path, sha256) — the hash is always
        `snapshot.sha256`, never recomputed, so the manifest's own claim and
        the bytes actually captured are provably the same value. Same
        idempotent-if-identical / raises-if-different behavior as
        `archive_transcript` for a re-archive under the same run_id, and the
        same `chmod 0444` convention.
        """
        dest_dir = self.run_dir(run_id) / ARCHIVE_DIRNAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / snapshot.source_path.name

        if dest.exists():
            existing = hashlib.sha256(dest.read_bytes()).hexdigest()
            if existing != snapshot.sha256:
                raise ValueError(
                    f"{dest} already archived with a different hash "
                    f"({existing[:12]}… vs {snapshot.sha256[:12]}…) — a run's raw "
                    f"source does not change after the fact"
                )
            return dest, snapshot.sha256

        dest.write_bytes(snapshot.content)
        # Read the just-written file back and hash it: disk-level write
        # integrity, not source-freshness (the source is already in-memory
        # bytes, so there is nothing "fresh" left to re-check there).
        written = hashlib.sha256(dest.read_bytes()).hexdigest()
        if written != snapshot.sha256:
            raise ValueError(
                f"{dest} write verification failed: wrote bytes hashing "
                f"{written[:12]}… but the snapshot declares {snapshot.sha256[:12]}… "
                f"— the archive write itself did not land correctly"
            )
        dest.chmod(0o444)
        return dest, snapshot.sha256

    def verify_archive(self, run_id: str) -> str | None:
        """Re-hash the archived transcript. Returns a problem, or None if intact."""
        _, _, manifest = self.read_run(run_id)
        declared = manifest.get("archived_transcript_sha256")
        path = manifest.get("archived_transcript_path")
        if not declared or not path:
            return f"run {run_id} has no archived transcript recorded"
        p = Path(path)
        if not p.exists():
            return f"archived transcript missing at {p}"
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != declared:
            return (f"archived transcript at {p} hashes {actual[:12]}… but the "
                    f"manifest declares {declared[:12]}…")
        return None

    def read_run(self, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        d = self.run_dir(run_id)
        events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines() if l.strip()]
        run = json.loads((d / "run.json").read_text())
        manifest = json.loads((d / "manifest.json").read_text())
        return events, run, manifest

    def list_runs(self) -> list[str]:
        runs = self.root / "runs"
        return sorted(p.name for p in runs.iterdir() if p.is_dir()) if runs.exists() else []


def build_manifest(
    *,
    result: Any,                  # AdapterResult | CodexAdapterResult
    reconciliation: dict[str, Any],
    raw_local_path: str | Path,
    task_id: str | None = None,
    policy_id: str | None = None,
    policy_version: str | None = None,
    policy_text_sha256: str | None = None,
    task_prompt_sha256: str | None = None,
    delivered_prompt_sha256: str | None = None,
    environment_fingerprint: dict[str, Any] | None = None,
    archived_transcript_path: str | None = None,
    archived_transcript_sha256: str | None = None,
    final_state_attestation: dict[str, Any] | None = None,
    trace_profile: str | None = None,
) -> RunManifest:
    """Assemble a manifest from an adapter result and its reconciliation report."""
    run = result.run
    reconciled_ok = all(v.get("ok") for v in reconciliation.values())
    return RunManifest(
        run_id=run["run_id"],
        source_client=result.events[0]["source_client"] if result.events else "unknown",
        source_client_version=(
            result.events[0].get("source_client_version") if result.events else None
        ),
        schema_version=run["schema_version"],
        event_count=len(result.events),
        session_snapshot_sha256=result.snapshot_sha256,
        raw_local_path=str(raw_local_path),
        task_id=task_id or run.get("task_id", "unknown"),
        pinned_commit=run["pinned_commit"],
        repo_url=run["repo_url"],
        policy_id=policy_id if policy_id is not None else run.get("policy_id"),
        policy_version=(
            policy_version if policy_version is not None else run.get("policy_version")
        ),
        policy_text_sha256=policy_text_sha256,
        task_prompt_sha256=task_prompt_sha256,
        model=run.get("model"),
        model_source=run.get("model_source"),
        model_evidence_consistent=run.get("model_evidence_consistent"),
        run_kind=run["run_kind"],
        condition=run["condition"],
        disposition=run["disposition"],
        disposition_reason=run.get("disposition_reason"),
        disposition_counts=dict(result.disposition_counts),
        preserved_detail=dict(result.preserved_detail),
        reconciliation=reconciliation,
        reconciled_ok=reconciled_ok,
        delivered_prompt_sha256=delivered_prompt_sha256,
        environment_fingerprint=environment_fingerprint,
        archived_transcript_path=archived_transcript_path,
        archived_transcript_sha256=archived_transcript_sha256,
        final_state_attestation=final_state_attestation,
        trace_profile=trace_profile,
    )
