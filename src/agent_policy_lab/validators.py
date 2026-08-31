"""Dataset-level validators for canonical event sets.

Everything here is a check JSON Schema structurally cannot make, because it
spans records rather than validating one. Each is a deliberate deferral from
the schema layer, not an optional extra; none may be dropped silently:

1. `sequence_index` uniqueness within a session
2. `run_id` referential integrity
3. `field_status` per-field cross-check
4. assistant-envelope accounting
5. unmatched tool-correlation counts

Checks 1-4 return findings (a finding means the data is wrong). Check 5 returns
*counts*, not findings: an interrupted session legitimately produces an orphan
call. Silence is the failure mode there, not the orphan itself.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

PAYLOAD_KEYS = (
    "tool_call",
    "tool_result",
    "thinking",
    "compaction",
    "assistant_message",
    "user_message",
)


@dataclass
class ValidationReport:
    findings: list[str] = field(default_factory=list)
    unmatched_calls: int = 0
    unmatched_results: int = 0
    assistant_envelopes: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings


def check_sequence_index_unique(events: Iterable[dict[str, Any]]) -> list[str]:
    per_session: dict[str, Counter] = {}
    for ev in events:
        per_session.setdefault(ev["session_id"], Counter())[ev["sequence_index"]] += 1
    findings = []
    for session_id, counter in per_session.items():
        dupes = sorted(i for i, n in counter.items() if n > 1)
        if dupes:
            findings.append(
                f"session {session_id}: duplicate sequence_index values {dupes} — "
                f"sequence_index must be a total order within a session"
            )
    return findings


def check_run_ids_resolve(
    events: Iterable[dict[str, Any]], runs: Iterable[dict[str, Any]]
) -> list[str]:
    known = {r["run_id"] for r in runs}
    dangling = sorted({ev["run_id"] for ev in events} - known)
    return [
        f"dangling run_id {rid} — no experiment_run record joins these events "
        f"to a treatment arm"
        for rid in dangling
    ]


def check_field_status_covers_payload(events: Iterable[dict[str, Any]]) -> list[str]:
    """Every key present in a typed payload has a field_status entry, and vice versa.

    This is the check the schema layer has always deferred to adapter time,
    and the reason it says not to call field_status "mechanically enforced"
    until it exists. A payload key with no status is a value whose provenance
    nobody declared — exactly the smoothing a per-field status map exists to
    prevent.
    """
    findings = []
    for ev in events:
        payloads = {k: ev[k] for k in PAYLOAD_KEYS if k in ev}
        if not payloads:
            continue
        present: set[str] = set()
        for payload in payloads.values():
            # result_size_unit is a fixed const, not a variable field, so it
            # carries no independent provenance.
            present |= {k for k in payload if k != "result_size_unit"}
        declared = set(ev.get("field_status", {}))
        eid = ev["event_id"]
        for missing in sorted(present - declared):
            findings.append(
                f"event {eid} ({ev['event_type']}): payload field {missing!r} has "
                f"no field_status entry"
            )
        for extra in sorted(declared - present):
            findings.append(
                f"event {eid} ({ev['event_type']}): field_status declares {extra!r}, "
                f"which is not present in any payload"
            )
    return findings


def check_assistant_envelope_accounting(events: Iterable[dict[str, Any]]) -> list[str]:
    """At most one assistant_message envelope per raw source line.

    The other half of this invariant — *exactly* one per normalized assistant
    line — lives in the adapter's raw-fixture tests, because it needs the raw
    input to know how many were expected. "At most one" is satisfied by zero,
    which would silently drop a response's token usage.
    """
    per_line: Counter = Counter()
    for ev in events:
        if ev["event_type"] != "assistant_message":
            continue
        ref = ev.get("raw_source_ref") or {}
        per_line[(ev["session_id"], ref.get("line_or_offset"))] += 1
    return [
        f"session {sid} line {line}: {n} assistant_message envelopes — token usage "
        f"would be counted {n} times for one response"
        for (sid, line), n in sorted(per_line.items(), key=lambda kv: str(kv[0]))
        if n > 1
    ]


def correlation_counts(events: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Unmatched call/result counts. Reported, never a finding — see module docstring."""
    calls, results = set(), set()
    for ev in events:
        if ev["event_type"] == "tool_call":
            cid = (ev.get("tool_call") or {}).get("tool_correlation_id")
            if cid:
                calls.add(cid)
        elif ev["event_type"] == "tool_result":
            cid = (ev.get("tool_result") or {}).get("tool_correlation_id")
            if cid:
                results.add(cid)
    return len(calls - results), len(results - calls)


def validate_dataset(
    events: list[dict[str, Any]], runs: list[dict[str, Any]]
) -> ValidationReport:
    report = ValidationReport()
    report.findings += check_sequence_index_unique(events)
    report.findings += check_run_ids_resolve(events, runs)
    report.findings += check_field_status_covers_payload(events)
    report.findings += check_assistant_envelope_accounting(events)
    report.unmatched_calls, report.unmatched_results = correlation_counts(events)
    report.assistant_envelopes = sum(
        1 for e in events if e["event_type"] == "assistant_message"
    )
    report.counts = dict(Counter(e["event_type"] for e in events))
    return report
