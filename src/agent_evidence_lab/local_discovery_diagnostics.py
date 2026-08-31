"""Local discovery diagnostics: six pure calculation functions.

Implements the six diagnostics documented by the local-discovery report
contract, in the shape enforced by its schema and validators.
Every `compute_*` function takes one `SessionDiagnosticInput` (one session's
own canonical events, plus -- for `reconciliation_availability` only -- that
session's own `reconcile()` output) and returns one per-session leaf. Every
`_aggregate_*` function takes the dict of per-session leaves already computed
for one diagnostic and returns the aggregate leaf. No I/O, no snapshotting, no
filesystem access anywhere in this module -- every function here is testable
with hand-built dicts alone.

`DIAGNOSTICS_SPEC` is the one place a diagnostic's name/measure_kind/
rule_version/signal_vocabulary and its `calculate`/`aggregate` callables are
declared together, so the two can't drift apart independently. It is a tuple
of frozen `DiagnosticSpec` records, not a mutable list -- `signal_vocabulary`
is a tuple too. Converting to JSON-serializable lists happens only once, in
`local_discovery_runner.py`, when a report is actually assembled.

`reconciliation_availability` is `measure_kind: "adapter_reconciliation"`,
not `"derived"` -- it combines snapshot-derived raw counts with canonical
emitted-event counts via the adapter's own reconciliation step, not a
canonical field with a `field_status`. Its value and provenance shapes are
defined in `local_discovery_report.py` (`RECONCILED_ORDINARY_KEYS`/
`RECONCILED_PRESERVED_KEYS`, `validate_reconciliation_shape`) and reused here,
not restated independently.

Every provenance input declares an explicit evidence `basis`,
one of three kinds (`local_discovery_report.py` defines and validates all
three): `payload_field_status` for a field namespaced by its event-type
payload (e.g. `assistant_message.input_tokens`, `tool_call.tool_name`) --
the schema's own field_status map tracks these. `envelope_timestamp_presence`
for `elapsed_event_span`'s bare, un-namespaced `timestamp` input -- a
top-level envelope field, never part of any payload's field_status map, so
its evidence is presence/absence (`status: observed | unavailable`), not a
borrowed `field_status`. `required_envelope_classification` for
`compaction_count`'s bare `event_type` input -- a schema-required field that
is always populated, never variable, so it carries no status key at all;
declaring one would claim a variability that doesn't exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from agent_evidence_lab.local_discovery_report import (
    ENVELOPE_TIMESTAMP_BASIS,
    PAYLOAD_FIELD_STATUS_BASIS,
    RECONCILED_ORDINARY_KEYS,
    RECONCILED_PRESERVED_KEYS,
    REQUIRED_ENVELOPE_CLASSIFICATION_BASIS,
)

RULE_VERSION = "1.0.0"


@dataclass(frozen=True)
class SessionDiagnosticInput:
    """One session's own data, and nothing else -- no session_id (a
    presentation detail the orchestrator attaches when assembling
    `per_session` keys, never a computation input)."""

    events: list[dict[str, Any]]
    reconciliation: dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# reconciliation_availability
# ---------------------------------------------------------------------------


def compute_reconciliation_availability(ctx: SessionDiagnosticInput) -> dict:
    r = ctx.reconciliation
    value: dict[str, Any] = {}
    for cls in RECONCILED_ORDINARY_KEYS:
        raw = r[cls]["raw"]
        canonical = r[cls]["emitted"]
        value[cls] = {"raw_count": raw, "canonical_count": canonical, "delta": raw - canonical}
    preserved: dict[str, Any] = {}
    for cat in RECONCILED_PRESERVED_KEYS:
        raw = r["_preserved"][f"{cat}_raw"]
        kept = r["_preserved"][f"{cat}_preserved"]
        preserved[cat] = {"raw_count": raw, "preserved_count": kept, "delta": raw - kept}
    value["preserved"] = preserved
    return {
        "outcome": "computed",
        "value": value,
        "provenance": {
            "rule_version": RULE_VERSION,
            "source": "adapter_reconciliation",
            "rule": (
                "raw_count/canonical_count/delta per ordinary class and turns, "
                "raw_count/preserved_count/delta per preserved category, converted "
                "from adapter_claude_code.reconcile()'s per-session output; ok is a "
                "fail-fast gate checked before this ran, not a measured value"
            ),
        },
    }


def _aggregate_reconciliation_availability(per_session: dict[str, dict]) -> dict:
    leaves = list(per_session.values())
    value: dict[str, Any] = {}
    for cls in RECONCILED_ORDINARY_KEYS:
        raw = sum(leaf["value"][cls]["raw_count"] for leaf in leaves)
        canonical = sum(leaf["value"][cls]["canonical_count"] for leaf in leaves)
        value[cls] = {"raw_count": raw, "canonical_count": canonical, "delta": raw - canonical}
    preserved: dict[str, Any] = {}
    for cat in RECONCILED_PRESERVED_KEYS:
        raw = sum(leaf["value"]["preserved"][cat]["raw_count"] for leaf in leaves)
        kept = sum(leaf["value"]["preserved"][cat]["preserved_count"] for leaf in leaves)
        preserved[cat] = {"raw_count": raw, "preserved_count": kept, "delta": raw - kept}
    value["preserved"] = preserved
    return {
        "outcome": "computed",
        "value": value,
        "provenance": {
            "rule_version": RULE_VERSION,
            "source": "adapter_reconciliation",
            "rule": (
                "sum raw_count and canonical_count (or preserved_count) across sessions "
                "per key, then recompute delta from the summed pair"
            ),
        },
    }


# ---------------------------------------------------------------------------
# assistant_token_usage
# ---------------------------------------------------------------------------


def compute_assistant_token_usage(ctx: SessionDiagnosticInput) -> dict:
    total_input = 0
    total_output = 0
    considered = 0
    excluded = 0
    for ev in ctx.events:
        if ev.get("event_type") != "assistant_message":
            continue
        considered += 1
        status = ev.get("field_status") or {}
        if status.get("input_tokens") == "observed" and status.get("output_tokens") == "observed":
            payload = ev["assistant_message"]
            total_input += payload["input_tokens"]
            total_output += payload["output_tokens"]
        else:
            excluded += 1
    if considered == excluded:
        return {
            "outcome": "not_reportable",
            "reason": (
                f"0 of {considered} assistant_message events in this session have both "
                "input_tokens and output_tokens observed"
            ),
        }
    return {
        "outcome": "computed",
        "value": {"input_tokens": total_input, "output_tokens": total_output},
        "provenance": {
            "inputs": [
                {"field": "assistant_message.input_tokens", "basis": PAYLOAD_FIELD_STATUS_BASIS, "field_status": "observed"},
                {"field": "assistant_message.output_tokens", "basis": PAYLOAD_FIELD_STATUS_BASIS, "field_status": "observed"},
            ],
            "rule_version": RULE_VERSION,
            "excluded_events": excluded,
        },
    }


def _aggregate_assistant_token_usage(per_session: dict[str, dict]) -> dict:
    computed = [leaf for leaf in per_session.values() if leaf["outcome"] == "computed"]
    total_input = sum(leaf["value"]["input_tokens"] for leaf in computed)
    total_output = sum(leaf["value"]["output_tokens"] for leaf in computed)
    return {
        "outcome": "computed",
        "value": {"total_input_tokens": total_input, "total_output_tokens": total_output},
        "provenance": {
            "rule_version": RULE_VERSION,
            "rule": (
                "sum of computed per-session input_tokens/output_tokens; "
                "not_reportable sessions contribute nothing to either total"
            ),
        },
    }


# ---------------------------------------------------------------------------
# elapsed_event_span
# ---------------------------------------------------------------------------


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_elapsed_event_span(ctx: SessionDiagnosticInput) -> dict:
    events = sorted(ctx.events, key=lambda e: e["sequence_index"])
    if len(events) < 2:
        return {"outcome": "not_reportable", "reason": "fewer than 2 events in this session"}
    first, last = events[0], events[-1]
    if first.get("timestamp") is None or last.get("timestamp") is None:
        return {
            "outcome": "not_reportable",
            "reason": "the first or last event's timestamp is not observed",
        }
    elapsed = int((_parse_timestamp(last["timestamp"]) - _parse_timestamp(first["timestamp"])).total_seconds())
    return {
        "outcome": "computed",
        "value": {"elapsed_seconds": elapsed},
        "provenance": {
            "inputs": [
                {"field": "timestamp", "basis": ENVELOPE_TIMESTAMP_BASIS, "status": "observed", "role": "first_event"},
                {"field": "timestamp", "basis": ENVELOPE_TIMESTAMP_BASIS, "status": "observed", "role": "last_event"},
            ],
            "rule_version": RULE_VERSION,
        },
    }


def _aggregate_elapsed_event_span(per_session: dict[str, dict]) -> dict:
    computed = [leaf for leaf in per_session.values() if leaf["outcome"] == "computed"]
    total = sum(leaf["value"]["elapsed_seconds"] for leaf in computed)
    return {
        "outcome": "computed",
        "value": {"total_elapsed_seconds": total},
        "provenance": {
            "rule_version": RULE_VERSION,
            "rule": "sum of computed per-session elapsed spans -- never a mean, never a single session's own span",
        },
    }


# ---------------------------------------------------------------------------
# compaction_count
# ---------------------------------------------------------------------------


def compute_compaction_count(ctx: SessionDiagnosticInput) -> dict:
    count = sum(1 for ev in ctx.events if ev.get("event_type") == "compaction")
    return {
        "outcome": "computed",
        "value": {"count": count},
        "provenance": {
            "inputs": [{"field": "event_type", "basis": REQUIRED_ENVELOPE_CLASSIFICATION_BASIS}],
            "rule_version": RULE_VERSION,
        },
    }


def _aggregate_compaction_count(per_session: dict[str, dict]) -> dict:
    total = sum(leaf["value"]["count"] for leaf in per_session.values())
    return {
        "outcome": "computed",
        "value": {"total_count": total},
        "provenance": {"rule_version": RULE_VERSION, "rule": "sum of per-session compaction counts"},
    }


# ---------------------------------------------------------------------------
# tool_invocation_distribution
# ---------------------------------------------------------------------------


def compute_tool_invocation_distribution(ctx: SessionDiagnosticInput) -> dict:
    counts: dict[str, int] = {}
    excluded = 0
    for ev in ctx.events:
        if ev.get("event_type") != "tool_call":
            continue
        status = ev.get("field_status") or {}
        if status.get("tool_name") != "observed":
            excluded += 1
            continue
        name = ev["tool_call"]["tool_name"]
        counts[name] = counts.get(name, 0) + 1
    return {
        "outcome": "computed",
        "value": counts,
        "provenance": {
            "inputs": [{"field": "tool_call.tool_name", "basis": PAYLOAD_FIELD_STATUS_BASIS, "field_status": "observed"}],
            "rule_version": RULE_VERSION,
            "rule": (
                "count of tool_call events per session grouped by tool_name; events "
                "with tool_name field_status not observed are excluded and tallied "
                "separately"
            ),
            "excluded_unknown_tool_name": excluded,
        },
    }


def _aggregate_tool_invocation_distribution(per_session: dict[str, dict]) -> dict:
    merged: dict[str, int] = {}
    excluded_total = 0
    for leaf in per_session.values():
        for name, count in leaf["value"].items():
            merged[name] = merged.get(name, 0) + count
        excluded_total += leaf["provenance"].get("excluded_unknown_tool_name", 0)
    return {
        "outcome": "computed",
        "value": merged,
        "provenance": {
            "rule_version": RULE_VERSION,
            "rule": "sum of per-session tool-name counts",
            "excluded_unknown_tool_name_total": excluded_total,
        },
    }


# ---------------------------------------------------------------------------
# paired_tool_outcome_observed_success
# ---------------------------------------------------------------------------


def compute_paired_tool_outcome(ctx: SessionDiagnosticInput) -> dict:
    results_by_correlation: dict[str, dict] = {}
    for ev in ctx.events:
        if ev.get("event_type") != "tool_result":
            continue
        corr = ev["tool_result"].get("tool_correlation_id")
        if corr is not None:
            results_by_correlation[corr] = ev

    success = 0
    failure = 0
    excluded = 0
    for ev in ctx.events:
        if ev.get("event_type") != "tool_call":
            continue
        corr = ev["tool_call"].get("tool_correlation_id")
        result_ev = results_by_correlation.get(corr) if corr is not None else None
        if result_ev is None:
            excluded += 1
            continue
        status = (result_ev.get("field_status") or {}).get("success")
        succ = result_ev["tool_result"].get("success")
        if status != "observed" or succ is None:
            excluded += 1
            continue
        if succ:
            success += 1
        else:
            failure += 1

    return {
        "outcome": "computed",
        "value": {"observed_success": success, "observed_failure": failure},
        "provenance": {
            "inputs": [
                {"field": "tool_call.tool_correlation_id", "basis": PAYLOAD_FIELD_STATUS_BASIS, "field_status": "inferred"},
                {"field": "tool_result.tool_correlation_id", "basis": PAYLOAD_FIELD_STATUS_BASIS, "field_status": "inferred"},
                {"field": "tool_result.success", "basis": PAYLOAD_FIELD_STATUS_BASIS, "field_status": "observed"},
            ],
            "rule_version": RULE_VERSION,
            "rule": (
                "join tool_call/tool_result on tool_correlation_id; count "
                "success/failure only where tool_result.success is observed"
            ),
            "excluded_unknown_success": excluded,
        },
    }


def _aggregate_paired_tool_outcome(per_session: dict[str, dict]) -> dict:
    success = sum(leaf["value"]["observed_success"] for leaf in per_session.values())
    failure = sum(leaf["value"]["observed_failure"] for leaf in per_session.values())
    excluded_total = sum(
        leaf["provenance"].get("excluded_unknown_success", 0) for leaf in per_session.values()
    )
    return {
        "outcome": "computed",
        "value": {"observed_success": success, "observed_failure": failure},
        "provenance": {
            "rule_version": RULE_VERSION,
            "rule": "sum of per-session success/failure counts",
            "excluded_unknown_success_total": excluded_total,
        },
    }


# ---------------------------------------------------------------------------
# DIAGNOSTICS_SPEC -- one genuine source of truth (declaration + dispatch)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagnosticSpec:
    name: str
    measure_kind: str  # "derived" | "adapter_reconciliation"
    rule_version: str
    signal_vocabulary: tuple[str, ...]
    calculate: Callable[[SessionDiagnosticInput], dict]
    aggregate: Callable[[dict[str, dict]], dict]


DIAGNOSTICS_SPEC: tuple[DiagnosticSpec, ...] = (
    DiagnosticSpec(
        "reconciliation_availability", "adapter_reconciliation", RULE_VERSION, (),
        compute_reconciliation_availability, _aggregate_reconciliation_availability,
    ),
    DiagnosticSpec(
        "assistant_token_usage", "derived", RULE_VERSION, (),
        compute_assistant_token_usage, _aggregate_assistant_token_usage,
    ),
    DiagnosticSpec(
        "elapsed_event_span", "derived", RULE_VERSION, (),
        compute_elapsed_event_span, _aggregate_elapsed_event_span,
    ),
    DiagnosticSpec(
        "compaction_count", "derived", RULE_VERSION, (),
        compute_compaction_count, _aggregate_compaction_count,
    ),
    DiagnosticSpec(
        "tool_invocation_distribution", "derived", RULE_VERSION, (),
        compute_tool_invocation_distribution, _aggregate_tool_invocation_distribution,
    ),
    DiagnosticSpec(
        "paired_tool_outcome_observed_success", "derived", RULE_VERSION, (),
        compute_paired_tool_outcome, _aggregate_paired_tool_outcome,
    ),
)
