"""Aggregate-only structural inventory of a local corpus.

It deliberately reuses the adapters and their independent reconciliation
contracts.  It keeps no normalized events or transcript text: each source is
snapshotted, processed in memory, and represented in the report only by its
content hash and aggregate structural counts.

This is not a behavioural detector.  In particular, it never examines tool
arguments, message text, paths, or candidate-pattern frequency.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from argparse import ArgumentParser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Literal

from . import adapter_claude_code, adapter_codex
from .snapshot import SessionSnapshot

SourceClient = Literal["claude-code", "codex-cli"]
_INVENTORY_NAMESPACE = uuid.UUID("3cf70428-018b-54d4-9be5-e63ce6a760ce")


@dataclass(frozen=True)
class InventorySource:
    """One local transcript to inspect.

    ``repo_root`` is only used by the Claude adapter to decide whether a tool
    target path is repo-relative.  The inventory reports no target paths and never scores
    their behaviour; callers may omit it, in which case the transcript's
    directory is used and such paths honestly become unavailable.
    """

    path: Path
    source_client: SourceClient
    repo_root: Path | None = None


@dataclass
class CohortInventory:
    source_client: str
    schema_version: str
    source_client_version: str | None
    session_count: int = 0
    normalized_sessions: int = 0
    reconciled_sessions: int = 0
    run_dispositions: Counter[str] = field(default_factory=Counter)
    raw_dispositions: Counter[str] = field(default_factory=Counter)
    preserved_detail: Counter[str] = field(default_factory=Counter)
    event_types: Counter[str] = field(default_factory=Counter)
    dates: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    snapshot_hashes: list[str] = field(default_factory=list)

    def snapshot_sha256(self) -> str:
        """Stable cohort identifier over source bytes, without source paths."""
        material = "\n".join(sorted(self.snapshot_hashes)).encode("ascii")
        return hashlib.sha256(material).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_client": self.source_client,
            "schema_version": self.schema_version,
            "source_client_version": self.source_client_version,
            "session_count": self.session_count,
            "normalized_sessions": self.normalized_sessions,
            "reconciled_sessions": self.reconciled_sessions,
            "run_dispositions": dict(sorted(self.run_dispositions.items())),
            "raw_dispositions": dict(sorted(self.raw_dispositions.items())),
            "preserved_detail": dict(sorted(self.preserved_detail.items())),
            "event_types": dict(sorted(self.event_types.items())),
            "dates": dict(sorted(self.dates.items())),
            "failures": dict(sorted(self.failures.items())),
            "cohort_snapshot_sha256": self.snapshot_sha256(),
        }


@dataclass
class StructuralInventory:
    """Inventory output.  Deliberately aggregate-only and safe to review locally."""

    cohorts: dict[tuple[str, str, str | None], CohortInventory] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohorts": [
                self.cohorts[key].to_dict()
                for key in sorted(self.cohorts, key=lambda k: (k[0], k[1], k[2] or ""))
            ]
        }


def _date_from_events(events: list[dict[str, Any]]) -> str:
    timestamp = next((event.get("timestamp") for event in events if event.get("timestamp")), None)
    if isinstance(timestamp, str) and len(timestamp) >= 10:
        try:
            return date.fromisoformat(timestamp[:10]).isoformat()
        except ValueError:
            pass
    return "unavailable"


def _cohort(
    report: StructuralInventory, *, source_client: str, schema_version: str,
    source_client_version: str | None,
) -> CohortInventory:
    key = (source_client, schema_version, source_client_version)
    return report.cohorts.setdefault(
        key,
        CohortInventory(source_client, schema_version, source_client_version),
    )


def _record_failure(
    report: StructuralInventory, source: InventorySource, snapshot: SessionSnapshot,
    exc: Exception,
) -> None:
    schema = "0.1.1" if source.source_client == "claude-code" else "0.2.0"
    cohort = _cohort(report, source_client=source.source_client, schema_version=schema,
                     source_client_version=None)
    cohort.session_count += 1
    cohort.snapshot_hashes.append(snapshot.sha256)
    # Class only: exception strings frequently contain local paths or raw types.
    cohort.failures[type(exc).__name__] += 1


def build_inventory(sources: Iterable[InventorySource]) -> StructuralInventory:
    """Snapshot, normalize, and reconcile sources without retaining Tier 1 data."""
    report = StructuralInventory()
    for source in sources:
        snapshot = SessionSnapshot.capture(source.path)
        run_id = str(uuid.uuid5(_INVENTORY_NAMESPACE, f"{source.source_client}|{snapshot.sha256}"))
        try:
            if source.source_client == "claude-code":
                result = adapter_claude_code.normalize_session(
                    snapshot, run_id=run_id, local_secret=b"m3-structural-inventory",
                    repo_root=source.repo_root or source.path.parent,
                    repo_ref=f"sha256:{snapshot.sha256}", task_id="m3-structural-inventory",
                )
                reconciliation = adapter_claude_code.reconcile(snapshot, result)
            elif source.source_client == "codex-cli":
                result = adapter_codex.normalize_session(
                    snapshot, run_id=run_id, local_secret=b"m3-structural-inventory",
                    repo_ref=f"sha256:{snapshot.sha256}",
                )
                reconciliation = adapter_codex.reconcile(snapshot, result)
            else:  # Defensive: callers cannot smuggle in an uncontracted client.
                raise ValueError(f"unsupported inventory source_client {source.source_client!r}")
        except Exception as exc:  # failures are structural results, not silent drops
            _record_failure(report, source, snapshot, exc)
            continue

        version = result.events[0].get("source_client_version") if result.events else None
        cohort = _cohort(report, source_client=source.source_client,
                         schema_version=result.run["schema_version"],
                         source_client_version=version)
        cohort.session_count += 1
        cohort.normalized_sessions += 1
        cohort.snapshot_hashes.append(snapshot.sha256)
        cohort.run_dispositions[result.run["disposition"]] += 1
        cohort.raw_dispositions.update(result.disposition_counts)
        cohort.preserved_detail.update(result.preserved_detail)
        cohort.event_types.update(event["event_type"] for event in result.events)
        cohort.dates[_date_from_events(result.events)] += 1
        if all(item.get("ok") for item in reconciliation.values()):
            cohort.reconciled_sessions += 1
        else:
            cohort.failures["ReconciliationMismatch"] += 1
    return report


def discover_sources(
    *,
    claude_root: Path = Path.home() / ".claude" / "projects",
    codex_root: Path = Path.home() / ".codex" / "sessions",
) -> list[InventorySource]:
    """Discover only known client transcript locations; do not inspect content."""
    sources: list[InventorySource] = []
    if claude_root.exists():
        sources.extend(InventorySource(path, "claude-code") for path in sorted(claude_root.rglob("*.jsonl")))
    if codex_root.exists():
        sources.extend(InventorySource(path, "codex-cli") for path in sorted(codex_root.rglob("*.jsonl")))
    return sources


def main() -> None:
    parser = ArgumentParser(description="Build the local-only structural inventory.")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    args = parser.parse_args()
    # stdout deliberately contains no source paths, session IDs, events, or text.
    print(json.dumps(build_inventory(discover_sources(
        claude_root=args.claude_root, codex_root=args.codex_root,
    )).to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
