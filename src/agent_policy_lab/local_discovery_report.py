"""Local discovery readiness report: validation and exclusive-create writer.

Implements only the generic report *contract* described in
`docs/local-discovery-report-contract.md` -- schema-shape validation,
structured-scope validation, uniqueness checks, results/availability/
per-session coverage validation, per-diagnostic provenance validation,
session-outcome-to-availability reconciliation, and closed
signal-vocabulary enforcement -- plus an exclusive, no-clobber, durable
writer with a local report-id registry.

This module does not read, capture, or normalize any session. It never
touches `~/.claude/projects` or any raw transcript. A caller builds the
report dict from wherever it likes (synthetic fixtures, in these tests;
real diagnostic output, eventually, from a separate module that reuses
`SessionSnapshot`/`adapter_claude_code`/`reconcile()`) and hands it here to be validated and durably, exclusively written.

One consistent result representation. Every diagnostic's result
is `{"per_session": {<session_id>: <leaf>, ...}, "aggregate": <leaf>}` --
`per_session` keyed *exactly* by every input session's id, `aggregate`
required (this v1 contract's six diagnostics all have a
meaningful aggregate, so an omitted one is incomplete, not terse). There
is no separate flat/global-only shape for some diagnostics and
per-session for others; a per-session leaf's `computed` outcome carries
full canonical-input provenance (it read real per-session fields), while
an aggregate leaf's `computed` outcome only needs a `rule_version` (it
summarizes already-validated per-session leaves, it does not read
canonical fields itself). Every `computed` leaf, per-session or
aggregate, must also carry a `value` -- "computed" means value +
provenance, not provenance alone.

This v1 contract's scope is fixed to exactly one developer, one
repository, and the Claude Code client (`validate_v1_scope_fixed`), and
hardens the writer: private (mode 0700) directories, complete (non-short)
writes, and a hygiene guard covering macOS/Linux/Windows home-path shapes.

A pre-use correction (prompted by the diagnostic runner design
review): `measure_kind` gains a third value, `"adapter_reconciliation"`,
for a measure that combines snapshot-derived raw counts with canonical
emitted-event counts via the adapter's own reconciliation step -- not a
canonical field with a `field_status`, so it is not validated as one. A
`derived` per-session `computed` leaf still needs `provenance.inputs`
(canonical fields + their own `field_status`); an `adapter_reconciliation`
per-session `computed` leaf instead needs `provenance.source ==
"adapter_reconciliation"`. `reconciliation_availability`'s own reported
shape is also now explicit and validated
(`validate_reconciliation_shape`): every ordinary reconciled class and
`turns` as `{raw_count, canonical_count, delta}`, every preserved
category (nested under `preserved`) as `{raw_count, preserved_count,
delta}` -- never the adapter's own internal `{emitted, raw, ok}`/
`_preserved` shape passed through verbatim, and never carrying `ok` as if
it were a measured value (it is a fail-fast gate, checked before any
diagnostic runs, not part of what was measured).

**Validator/runner boundary:**
this module guarantees report *shape and internal consistency* --
schema conformance, id/name uniqueness, coverage, availability
reconciliation against declared per-session outcomes, presence of
value+provenance on every computed leaf, presence of an aggregate for
every diagnostic, and signal-vocabulary membership. It does **not**, and
structurally cannot, verify that a computed value is numerically correct
against real per-session events, or that an aggregate actually equals the
sum (or other defined reduction) of its own per-session values -- this
module has no access to canonical events at all. Proving *that* is the
future diagnostic runner's job, via its own per-diagnostic unit tests
once it exists; it is not yet built, and nothing here claims otherwise.

The writer refuses any root or report_path inside the repository working
tree, mirroring `store.py`'s Tier 1 boundary (the publication boundary:
gitignoring is not the control).
"""

from __future__ import annotations

import json
import sys
import os
import re
import stat
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPORT_VERSION = "1.0.0"
SUPPORTED_REPORT_VERSIONS = frozenset({REPORT_VERSION})
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SAFE_REPORT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# This v1 contract is fixed to one developer, one repository, and the Claude
# Code client. A cross-client or multi-user report needs its own separately versioned
# contract, not a permissive value accepted here.
V1_FIXED_SCOPE = {
    "subject_count": 1,
    "repository_count": 1,
    "source_client": "claude-code",
}

# The exact classes/categories an "adapter_reconciliation" diagnostic's
# value must report on -- one place, shared by the shape validator and (per the
# diagnostic runner) the diagnostic that produces this shape, so neither
# can drift from the other by restating the list separately.
RECONCILED_ORDINARY_KEYS = (
    "tool_call", "tool_result", "thinking",
    "user_message", "assistant_message", "compaction", "turns",
)
RECONCILED_PRESERVED_KEYS = (
    "prompt_id_only_envelope",
    "user_record_without_prompt_source",
    "meta_injection",
)
_ORDINARY_TRIPLET_KEYS = ("raw_count", "canonical_count", "delta")
_PRESERVED_TRIPLET_KEYS = ("raw_count", "preserved_count", "delta")

# A provenance input's evidence basis is explicit and one of exactly
# three kinds -- payload fields (namespaced "<event_type>.<field>", tracked by the
# schema's own field_status map) are evidenced differently from envelope/structural
# fields (bare names, not namespaced, not part of any payload's field_status map).
# Conflating the two -- e.g. writing {"field": "event_type", "field_status": "observed"}
# as if a schema-required envelope classification were a variable payload field --
# is exactly the bug this closes.
PAYLOAD_FIELD_STATUS_BASIS = "payload_field_status"
ENVELOPE_TIMESTAMP_BASIS = "envelope_timestamp_presence"
REQUIRED_ENVELOPE_CLASSIFICATION_BASIS = "required_envelope_classification"
VALID_PROVENANCE_INPUT_BASES = frozenset(
    {PAYLOAD_FIELD_STATUS_BASIS, ENVELOPE_TIMESTAMP_BASIS, REQUIRED_ENVELOPE_CLASSIFICATION_BASIS}
)
_PAYLOAD_FIELD_STATUS_VALUES = frozenset({"observed", "inferred", "unavailable"})
# Deliberately narrower than the payload vocabulary: a bare timestamp is either
# present or it isn't -- there is no "inferred" concept for it the way a
# reconstructed payload value can be.
_ENVELOPE_TIMESTAMP_STATUS_VALUES = frozenset({"observed", "unavailable"})

# The dotted-vs-bare rule only tells payload fields apart from envelope
# fields in general -- it does not, by itself, say which *specific* bare field each
# envelope basis is for. Left unconstrained, an envelope basis would silently accept
# any bare field at all (including the other envelope basis's own field), which is
# exactly the same "passing one thing off as another" bug already closed for
# payload-vs-envelope, just one level down. Each envelope basis is fixed, in this v1
# contract, to exactly the one bare field it was designed for -- fail closed, not
# open, for any other bare field name. A future envelope field is a new or extended
# basis, added deliberately with its own documentation and tests, never inherited
# silently by widening one of these sets.
_ENVELOPE_BASIS_ALLOWED_FIELDS = {
    ENVELOPE_TIMESTAMP_BASIS: "timestamp",
    REQUIRED_ENVELOPE_CLASSIFICATION_BASIS: "event_type",
}

def _schema_path() -> Path:
    """Find the report contract in a checkout or installed wheel."""
    filename = "local_discovery_report.schema.json"
    candidates = (
        Path(__file__).resolve().parents[2] / "schema" / filename,
        Path(sys.prefix) / "schema" / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("local discovery report schema is not installed")


_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = Path.home() / ".agent-policy-lab" / "local_discovery"


class LocalDiscoveryPathError(Exception):
    """Refused a local discovery root or report_path inside the repository working tree,
    or a report_path outside the declared root -- the writer is Tier-1-root-only."""


class LocalDiscoveryUnsafeReportId(Exception):
    """report_id is not a safe filename/token -- it must contain only letters, digits,
    '_' and '-', so it can never be a path, contain a dot-segment, or traverse a
    directory when used to derive a filename. Checked explicitly (not only via the
    schema pattern) so the failure mode has its own clear, dedicated exception."""


class LocalDiscoveryDuplicateName(Exception):
    """input_sessions has a duplicate session_id, or diagnostics has a duplicate name."""


class LocalDiscoveryScopeMismatch(Exception):
    """scope.session_count does not equal len(input_sessions)."""


class LocalDiscoveryScopeNotSupported(Exception):
    """scope declares a subject_count, repository_count, or source_client this v1
    single-developer/single-repository/Claude-Code-only contract does not support.
    A cross-client or multi-user report requires its own separately versioned
    contract, not a permissive value accepted here."""


class LocalDiscoveryCoverageMismatch(Exception):
    """availability, results, or a single result's per_session does not have exactly
    one entry per diagnostic (or, for per_session, per input session) -- no diagnostic
    or session may be silently missing or silently extra."""


class LocalDiscoveryProvenanceInvalid(Exception):
    """A result leaf is missing its required provenance record for its outcome/kind."""


class LocalDiscoveryProvenanceBasisInvalid(Exception):
    """A provenance input's evidence basis is missing, unrecognized, mismatched with
    its own field (a payload field claiming an envelope basis, or an envelope field
    claiming the payload basis), claims an envelope basis for a bare field other than
    the one specific field that basis is fixed to in this v1 contract (e.g.
    'event_type' under envelope_timestamp_presence, 'timestamp' under
    required_envelope_classification, or any other bare field under either), or
    carries more than one evidence basis's companion key at once (exactly
    one basis per input, never zero, never two)."""


class LocalDiscoveryReconciliationShapeInvalid(Exception):
    """An adapter_reconciliation measure's computed value does not have the exact,
    explicit {raw_count, canonical_count, delta} / {raw_count, preserved_count, delta}
    shape this report contract requires -- never the adapter's own internal
    {emitted, raw, ok} shape passed through verbatim, and never carrying `ok` as if
    it were itself a measured value."""


class LocalDiscoveryAvailabilityMismatch(Exception):
    """An availability count tuple does not internally sum, does not reconcile against
    scope.session_count, or does not reconcile against its diagnostic's actual
    per-session outcome counts."""


class LocalDiscoverySignalNotDeclared(Exception):
    """A signal:<id> outcome does not match that diagnostic's declared signal_vocabulary."""


class LocalDiscoveryDuplicateReport(Exception):
    """report_id or report_path collides with an existing, already-registered report."""


class LocalDiscoveryWriteError(OSError):
    """os.write() returned zero or a negative byte count -- no forward progress is
    possible. A project-specific OSError subclass, not a silent infinite loop."""


class UnsupportedReportVersion(Exception):
    """report_version is missing, malformed, or not supported by this build.

    Deliberately a small self-contained check rather than a new entry in
    `versions.py`'s shared SUPPORTED table: that file is one of the frozen
    frozen executable dependencies (`stage_f5/FROZEN_HASHES.json`), and editing
    it to add an unrelated key breaks a content-hash drift check elsewhere.
    Same fail-closed shape as
    `versions.require_version`, without touching a file outside this contract's
    own boundary.
    """


def require_report_version(value: object) -> None:
    if not isinstance(value, str) or not _SEMVER.match(value):
        raise UnsupportedReportVersion(
            f"report_version={value!r} is missing or not a semantic version"
        )
    if value not in SUPPORTED_REPORT_VERSIONS:
        raise UnsupportedReportVersion(
            f"report_version={value!r} is not supported by this build "
            f"(supported: {sorted(SUPPORTED_REPORT_VERSIONS)}) -- refusing rather than "
            "reading it with assumptions from another version"
        )


def _load_schema() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_schema_path().read_text()))


def validate_schema_shape(report: dict[str, Any]) -> None:
    """Structural validation only -- required fields, types, enums. See module docstring
    for why cross-field semantics are validated separately, not folded into the schema."""
    _load_schema().validate(report)


def validate_report_id_safe(report: dict[str, Any]) -> None:
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or not _SAFE_REPORT_ID.match(report_id):
        raise LocalDiscoveryUnsafeReportId(
            f"report_id={report_id!r} is not a safe filename token -- only letters, "
            "digits, '_' and '-' are allowed (no '/', '\\', '.', or path-traversal "
            "characters)"
        )


def validate_unique_session_ids(report: dict[str, Any]) -> None:
    ids = [s["session_id"] for s in report["input_sessions"]]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise LocalDiscoveryDuplicateName(f"input_sessions has duplicate session_id(s): {dupes}")


def validate_unique_diagnostic_names(report: dict[str, Any]) -> None:
    names = [d["name"] for d in report["diagnostics"]]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise LocalDiscoveryDuplicateName(f"diagnostics has duplicate name(s): {dupes}")


def validate_scope(report: dict[str, Any]) -> None:
    scope = report["scope"]
    input_sessions = report["input_sessions"]
    if scope["session_count"] != len(input_sessions):
        raise LocalDiscoveryScopeMismatch(
            f"scope.session_count={scope['session_count']!r} does not match "
            f"len(input_sessions)={len(input_sessions)!r}"
        )


def validate_v1_scope_fixed(report: dict[str, Any]) -> None:
    scope = report["scope"]
    for field, expected in V1_FIXED_SCOPE.items():
        if scope[field] != expected:
            raise LocalDiscoveryScopeNotSupported(
                f"scope.{field}={scope[field]!r} is not supported by this v1 contract "
                f"(fixed at {expected!r} -- single developer, single repository, "
                "Claude Code only); a different value requires a separately versioned "
                "contract, not a permissive value in this one"
            )


def validate_availability_coverage(report: dict[str, Any]) -> None:
    declared = {d["name"] for d in report["diagnostics"]}
    have = set(report["availability"].keys())
    if declared != have:
        raise LocalDiscoveryCoverageMismatch(
            "availability must have exactly one entry per declared diagnostic "
            f"(missing={sorted(declared - have)}, extra={sorted(have - declared)})"
        )


def validate_coverage(report: dict[str, Any]) -> None:
    declared = {d["name"] for d in report["diagnostics"]}
    reported = set(report["results"].keys())
    missing = declared - reported
    extra = reported - declared
    if missing or extra:
        raise LocalDiscoveryCoverageMismatch(
            "results must have exactly one entry per declared diagnostic "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def validate_per_session_coverage(report: dict[str, Any]) -> None:
    session_ids = {s["session_id"] for s in report["input_sessions"]}
    for name, result in report["results"].items():
        keys = set(result["per_session"].keys())
        if keys != session_ids:
            raise LocalDiscoveryCoverageMismatch(
                f"{name}: per_session keys must exactly equal input_sessions' session ids "
                f"(missing={sorted(session_ids - keys)}, extra={sorted(keys - session_ids)})"
            )


def _count_tuple_sums(tup: dict[str, int]) -> bool:
    return tup["computed"] + tup["not_reportable"] + tup["excluded"] == tup["considered"]


def validate_availability_tuples(report: dict[str, Any]) -> None:
    session_count = report["scope"]["session_count"]
    for name, entry in report["availability"].items():
        sessions = entry["sessions"]
        if not _count_tuple_sums(sessions):
            raise LocalDiscoveryAvailabilityMismatch(
                f"{name}: sessions tuple {sessions} does not sum to its own considered count"
            )
        if sessions["considered"] != session_count:
            raise LocalDiscoveryAvailabilityMismatch(
                f"{name}: sessions.considered={sessions['considered']!r} does not match "
                f"scope.session_count={session_count!r}"
            )


def validate_session_outcome_reconciliation(report: dict[str, Any]) -> None:
    """The declared availability tuple must match what the per_session entries actually
    say, not just be internally self-consistent (validate_availability_tuples checks
    the latter; this checks the former).

    Convention (documented here because the count tuple has no dedicated 'signal'
    bucket): a per-session outcome of computed/not_reportable is tallied into the
    tuple's own computed/not_reportable fields; a per-session signal:<id> outcome is
    tallied into 'excluded' -- the tuple's catch-all for "this session did not land in
    either of the two ordinary buckets."
    """
    for name, result in report["results"].items():
        per_session = result["per_session"]
        computed = sum(1 for e in per_session.values() if e["outcome"] == "computed")
        not_reportable = sum(1 for e in per_session.values() if e["outcome"] == "not_reportable")
        signal = sum(1 for e in per_session.values() if e["outcome"].startswith("signal:"))
        tup = report["availability"][name]["sessions"]
        if computed != tup["computed"] or not_reportable != tup["not_reportable"]:
            raise LocalDiscoveryAvailabilityMismatch(
                f"{name}: per_session outcomes (computed={computed}, "
                f"not_reportable={not_reportable}, signal={signal}) do not reconcile "
                f"with the declared sessions tuple {tup}"
            )
        if signal != tup["excluded"]:
            raise LocalDiscoveryAvailabilityMismatch(
                f"{name}: {signal} per-session signal outcome(s) but "
                f"sessions.excluded={tup['excluded']!r} -- signal outcomes are tallied "
                "under 'excluded' by convention (see this function's docstring)"
            )


def _leaf_entries(result: dict[str, Any]):
    """Yield (kind, entry) for every provenance-bearing leaf in a result: every
    per_session entry, tagged 'per_session', and the aggregate entry if present,
    tagged 'aggregate'. The two kinds get different provenance requirements --
    see validate_provenance."""
    for entry in result["per_session"].values():
        yield "per_session", entry
    aggregate = result.get("aggregate")
    if aggregate is not None:
        yield "aggregate", aggregate


def _validate_provenance_input(name: str, kind: str, inp: dict[str, Any]) -> None:
    """Exactly one evidence basis per input, and each envelope basis fixed
    to its one specific bare field, fail closed.

    A payload field's name is namespaced by its event-type payload
    ("assistant_message.input_tokens", "tool_call.tool_name") -- this codebase's own
    convention, and the only fields the schema's field_status map actually tracks.
    A bare, un-namespaced field name ("event_type", "timestamp") is an envelope/
    structural field, never part of any payload's field_status map, and must use one
    of the two envelope bases instead. Neither basis's companion key may appear on
    an input using the other basis -- that mixing is exactly "passing one off as
    the other."

    The dotted-vs-bare rule only separates payload fields from envelope fields in
    general; it says nothing about which bare field a given envelope basis is for.
    That gap is closed: envelope_timestamp_presence is permitted only for
    "timestamp" and required_envelope_classification only for "event_type" --
    _ENVELOPE_BASIS_ALLOWED_FIELDS is the single source of truth for both. Any other
    bare field under either basis (including the other basis's own field) is
    refused, not silently accepted -- a future envelope field needs its own new or
    extended basis, added deliberately with documentation and tests.
    """
    field = inp.get("field")
    if not field:
        raise LocalDiscoveryProvenanceInvalid(
            f"{name} ({kind}): every provenance input needs its own field"
        )
    basis = inp.get("basis")
    if basis not in VALID_PROVENANCE_INPUT_BASES:
        raise LocalDiscoveryProvenanceBasisInvalid(
            f"{name} ({kind}): provenance input {field!r} must declare a basis, one "
            f"of {sorted(VALID_PROVENANCE_INPUT_BASES)}, got {basis!r}"
        )

    is_payload_field = "." in field
    if is_payload_field and basis != PAYLOAD_FIELD_STATUS_BASIS:
        raise LocalDiscoveryProvenanceBasisInvalid(
            f"{name} ({kind}): {field!r} is a payload field (namespaced by its event "
            f"type) and must use basis {PAYLOAD_FIELD_STATUS_BASIS!r}, not {basis!r} "
            "-- an envelope/structural basis cannot be used for a payload field"
        )
    if not is_payload_field and basis == PAYLOAD_FIELD_STATUS_BASIS:
        raise LocalDiscoveryProvenanceBasisInvalid(
            f"{name} ({kind}): {field!r} is a structural/envelope field and cannot "
            f"be passed off as {PAYLOAD_FIELD_STATUS_BASIS!r} -- use "
            f"{ENVELOPE_TIMESTAMP_BASIS!r} or {REQUIRED_ENVELOPE_CLASSIFICATION_BASIS!r} "
            "instead"
        )

    allowed_field = _ENVELOPE_BASIS_ALLOWED_FIELDS.get(basis)
    if allowed_field is not None and field != allowed_field:
        raise LocalDiscoveryProvenanceBasisInvalid(
            f"{name} ({kind}): basis {basis!r} is permitted only for the bare field "
            f"{allowed_field!r} in this v1 contract, not {field!r} -- a future "
            "envelope field requires an explicit new or extended basis, documented "
            "and tested, never inherited silently by widening this one"
        )

    has_field_status = "field_status" in inp
    has_envelope_status = "status" in inp
    if basis == PAYLOAD_FIELD_STATUS_BASIS:
        if not has_field_status or inp["field_status"] not in _PAYLOAD_FIELD_STATUS_VALUES:
            raise LocalDiscoveryProvenanceBasisInvalid(
                f"{name} ({kind}): {field!r} (basis={basis!r}) must carry its own "
                f"field_status, one of {sorted(_PAYLOAD_FIELD_STATUS_VALUES)}"
            )
        if has_envelope_status:
            raise LocalDiscoveryProvenanceBasisInvalid(
                f"{name} ({kind}): {field!r} (basis={basis!r}) must not also carry an "
                "envelope 'status' -- exactly one evidence basis is allowed per input"
            )
    elif basis == ENVELOPE_TIMESTAMP_BASIS:
        if not has_envelope_status or inp["status"] not in _ENVELOPE_TIMESTAMP_STATUS_VALUES:
            raise LocalDiscoveryProvenanceBasisInvalid(
                f"{name} ({kind}): {field!r} (basis={basis!r}) must carry its own "
                f"status, one of {sorted(_ENVELOPE_TIMESTAMP_STATUS_VALUES)}"
            )
        if has_field_status:
            raise LocalDiscoveryProvenanceBasisInvalid(
                f"{name} ({kind}): {field!r} (basis={basis!r}) must not also carry a "
                "payload field_status -- exactly one evidence basis is allowed per input"
            )
    elif basis == REQUIRED_ENVELOPE_CLASSIFICATION_BASIS:
        if has_field_status or has_envelope_status:
            raise LocalDiscoveryProvenanceBasisInvalid(
                f"{name} ({kind}): {field!r} (basis={basis!r}) is always populated by "
                "schema and never variable -- it must not carry field_status or "
                "status at all"
            )


def validate_provenance(report: dict[str, Any]) -> None:
    diagnostics_by_name = {d["name"]: d for d in report["diagnostics"]}
    for name, result in report["results"].items():
        diagnostic = diagnostics_by_name.get(name)
        if diagnostic is None:
            continue  # already caught by validate_coverage
        for kind, entry in _leaf_entries(result):
            outcome = entry["outcome"]
            if outcome == "not_reportable":
                if not entry.get("reason"):
                    raise LocalDiscoveryProvenanceInvalid(
                        f"{name} ({kind}): a not_reportable result must state its reason"
                    )
            elif outcome == "computed":
                if "value" not in entry:
                    raise LocalDiscoveryProvenanceInvalid(
                        f"{name} ({kind}): a computed result must carry a value -- "
                        "'computed' means value + provenance, not provenance alone"
                    )
                provenance = entry.get("provenance")
                if not provenance or not provenance.get("rule_version"):
                    raise LocalDiscoveryProvenanceInvalid(
                        f"{name} ({kind}): a computed result must state a rule_version"
                    )
                if kind == "per_session" and diagnostic["measure_kind"] == "derived":
                    if not provenance.get("inputs"):
                        raise LocalDiscoveryProvenanceInvalid(
                            f"{name} ({kind}): a derived per-session measure's computed "
                            "result must list its canonical inputs"
                        )
                    for inp in provenance["inputs"]:
                        _validate_provenance_input(name, kind, inp)
                elif kind == "per_session" and diagnostic["measure_kind"] == "adapter_reconciliation":
                    if provenance.get("source") != "adapter_reconciliation":
                        raise LocalDiscoveryProvenanceInvalid(
                            f"{name} ({kind}): an adapter_reconciliation measure's "
                            "computed result must state provenance.source == "
                            "'adapter_reconciliation' -- it is not a canonical field "
                            "with a field_status, and must not be labeled as one"
                        )
            # signal:<id> outcomes are checked by validate_signal_vocabulary, not here.


def _validate_triplet(name: str, kind: str, label: str, value: Any, expected_keys: tuple[str, ...]) -> None:
    if not isinstance(value, dict) or set(value.keys()) != set(expected_keys):
        raise LocalDiscoveryReconciliationShapeInvalid(
            f"{name} ({kind}): {label} must have exactly {sorted(expected_keys)}, "
            f"got {sorted(value.keys()) if isinstance(value, dict) else value!r}"
        )
    if not all(isinstance(value[k], int) and not isinstance(value[k], bool) for k in expected_keys):
        raise LocalDiscoveryReconciliationShapeInvalid(
            f"{name} ({kind}): {label}'s fields must all be integers, got {value!r}"
        )


def validate_reconciliation_shape(report: dict[str, Any]) -> None:
    """Every adapter_reconciliation diagnostic's computed value -- per-session and
    aggregate alike -- must have exactly RECONCILED_ORDINARY_KEYS (each a
    {raw_count, canonical_count, delta} triplet) plus a 'preserved' key holding
    exactly RECONCILED_PRESERVED_KEYS (each a {raw_count, preserved_count, delta}
    triplet). Never the adapter's own {emitted, raw, ok} shape, never an 'ok' field
    anywhere in the value (see LocalDiscoveryReconciliationShapeInvalid)."""
    diagnostics_by_name = {d["name"]: d for d in report["diagnostics"]}
    for name, result in report["results"].items():
        diagnostic = diagnostics_by_name.get(name)
        if diagnostic is None or diagnostic["measure_kind"] != "adapter_reconciliation":
            continue
        for kind, entry in _leaf_entries(result):
            if entry["outcome"] != "computed":
                continue
            value = entry.get("value")
            if not isinstance(value, dict):
                raise LocalDiscoveryReconciliationShapeInvalid(
                    f"{name} ({kind}): computed value must be an object"
                )
            expected_keys = set(RECONCILED_ORDINARY_KEYS) | {"preserved"}
            if set(value.keys()) != expected_keys:
                raise LocalDiscoveryReconciliationShapeInvalid(
                    f"{name} ({kind}): value must have exactly {sorted(expected_keys)}, "
                    f"got {sorted(value.keys())}"
                )
            for cls in RECONCILED_ORDINARY_KEYS:
                _validate_triplet(name, kind, cls, value[cls], _ORDINARY_TRIPLET_KEYS)
            preserved = value["preserved"]
            if not isinstance(preserved, dict) or set(preserved.keys()) != set(RECONCILED_PRESERVED_KEYS):
                raise LocalDiscoveryReconciliationShapeInvalid(
                    f"{name} ({kind}): preserved must have exactly "
                    f"{sorted(RECONCILED_PRESERVED_KEYS)}, got "
                    f"{sorted(preserved.keys()) if isinstance(preserved, dict) else preserved!r}"
                )
            for cat in RECONCILED_PRESERVED_KEYS:
                _validate_triplet(name, kind, f"preserved.{cat}", preserved[cat], _PRESERVED_TRIPLET_KEYS)


def validate_signal_vocabulary(report: dict[str, Any]) -> None:
    diagnostics_by_name = {d["name"]: d for d in report["diagnostics"]}
    for name, result in report["results"].items():
        diagnostic = diagnostics_by_name.get(name)
        vocabulary = set(diagnostic["signal_vocabulary"]) if diagnostic else set()
        for kind, entry in _leaf_entries(result):
            outcome = entry["outcome"]
            if outcome.startswith("signal:"):
                signal_id = outcome.split(":", 1)[1]
                if signal_id not in vocabulary:
                    raise LocalDiscoverySignalNotDeclared(
                        f"{name} ({kind}): signal:{signal_id!r} is not in this "
                        f"diagnostic's declared signal_vocabulary {sorted(vocabulary)} "
                        "-- an empty or non-matching vocabulary refuses every signal "
                        "outcome"
                    )


def validate_report(report: dict[str, Any]) -> None:
    """Full validation pipeline. Fails closed on the first violation found."""
    require_report_version(report.get("report_version"))
    validate_schema_shape(report)
    validate_report_id_safe(report)
    validate_unique_session_ids(report)
    validate_unique_diagnostic_names(report)
    validate_scope(report)
    validate_v1_scope_fixed(report)
    validate_availability_coverage(report)
    validate_coverage(report)
    validate_per_session_coverage(report)
    validate_availability_tuples(report)
    validate_session_outcome_reconciliation(report)
    validate_provenance(report)
    validate_reconciliation_shape(report)
    validate_signal_vocabulary(report)


def _assert_outside_repo(path: Path) -> None:
    try:
        path.relative_to(_REPO_ROOT)
    except ValueError:
        return  # outside the tree, which is the requirement
    raise LocalDiscoveryPathError(
        f"{path} is inside the repository at {_REPO_ROOT}. Local discovery reports "
        "must live outside the working tree -- gitignoring is not the control "
        "(the publication boundary), mirroring store.py's Tier 1 boundary."
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """os.write() may perform a short write (write fewer bytes than given) -- loop
    until every byte is actually written, rather than trusting one call. A single
    os.write() call is not "the durable write" the rest of this module's fsync
    discipline assumes; this is.

    Fails closed on a zero-or-negative write: os.write() returning 0 (or, per its
    documented contract, a negative value) means no forward progress is possible, and
    looping on that would hang forever rather than fail. Raise instead of retrying
    blindly.
    """
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise LocalDiscoveryWriteError(
                f"os.write() returned {n!r} for a {len(view)}-byte remaining write -- "
                "no forward progress is possible; refusing to loop forever"
            )
        view = view[n:]


def _ensure_private_dir(path: Path) -> None:
    """Ensure `path` exists as a private (mode 0700) directory.

    If `path` already exists, refuse (rather than silently trust) if its permissions
    are broader than 0700 -- Tier 1 local-discovery data must not end up inside a
    group- or world-readable directory by default. Missing parents *above* `path`
    itself are created with ordinary mkdir(parents=True) semantics: only `path` is a
    directory this function specifically promises to keep private (the three call
    sites are the discovery root, its _index directory, and a report's own parent --
    not arbitrary ancestors outside this module's control, like the user's home
    directory).
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)  # mkdir's mode argument is affected by umask
            return
        except FileExistsError:
            pass  # lost a concurrent create race -- fall through and verify the winner
    if not path.is_dir():
        raise LocalDiscoveryPathError(
            f"{path} already exists and is not a directory -- refusing to use it as "
            "a Tier 1 local-discovery directory (a permission-bit check alone would "
            "have passed here and failed later, indirectly, as NotADirectoryError)"
        )
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise LocalDiscoveryPathError(
            f"{path} exists with mode {oct(mode)}, broader than the required "
            "0700 -- refusing to write Tier 1 local-discovery data into a "
            "group- or world-readable directory"
        )


def _write_file_durable(path: Path, content: str, mode: int = 0o600) -> None:
    """Exclusive create (mode 0600), complete write, fsync the file, then fsync its
    directory.

    Durable against a crash *after* this returns: both the file's own content and the
    directory entry pointing to it have been flushed to storage, not just handed to the
    OS page cache. Raises FileExistsError untouched if `path` already exists -- callers
    decide what that means (a duplicate id, a duplicate path, or a genuine bug).
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        _write_all(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _create_index_entry(index_path: Path, report_id: str, final_path: Path) -> None:
    """Reserve report_id durably, before anything else is written.

    Status starts 'reserved' -- a deliberately non-terminal state. If this process is
    interrupted before the entry advances to 'written' or 'failed' (see
    _update_index_status), the entry stays 'reserved' forever, which is the honest
    outcome: nothing here claims a report was completed when its body's fate is
    actually unknown.
    """
    payload = json.dumps(
        {"report_id": report_id, "report_path": str(final_path), "status": "reserved"}
    )
    try:
        _write_file_durable(index_path, payload)
    except FileExistsError as exc:
        raise LocalDiscoveryDuplicateReport(
            f"report_id {report_id!r} is already registered in the local discovery "
            "index -- refusing to write a second report under the same id"
        ) from exc


def _update_index_status(
    index_dir: Path, index_path: Path, report_id: str, final_path: Path, status: str
) -> None:
    """Advance an already-reserved index entry to a terminal state ('written' or
    'failed'). Only the caller that successfully created the 'reserved' entry ever
    calls this for that report_id, so there is no exclusivity race here -- the atomic
    step that matters (claiming report_id) already happened in _create_index_entry.
    """
    payload = json.dumps(
        {"report_id": report_id, "report_path": str(final_path), "status": status}
    )
    tmp = index_dir / f".{report_id}.index.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _write_all(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, index_path)  # same-directory rename, atomic
    _fsync_dir(index_dir)


def create_report(report: dict[str, Any], root: str | Path = DEFAULT_ROOT) -> Path:
    """Validate `report`, then write it exclusively, atomically, and durably under `root`.

    Two independent exclusivity guarantees (write-temp-then-rename does not refuse a
    duplicate destination on POSIX, since rename silently replaces -- this
    correction):

    1. A durable index entry keyed by report_id is created with an
       O_CREAT|O_EXCL-equivalent open *before* the report body is written, status
       'reserved' -- this is what actually catches the same report_id being reused,
       even under a different report_path.
    2. The report body is linked into report_path with `os.link`, atomic and
       FileExistsError on an existing destination -- catches a duplicate report_path
       independently of report_id.

    The index entry advances to 'written' on success or 'failed' if the body write or
    link fails after being reserved -- never left silently implying success. All
    created files are mode 0600; the root, its _index directory, and a report's own
    parent directory are created (or verified) at mode 0700.
    """
    root = Path(root).expanduser().resolve()
    _assert_outside_repo(root)

    validate_report(report)

    report_id = report["report_id"]
    final_path = Path(report["report_path"]).expanduser().resolve()
    _assert_outside_repo(final_path)
    try:
        final_path.relative_to(root)
    except ValueError as exc:
        raise LocalDiscoveryPathError(
            f"report_path {final_path} is not under the local discovery root {root} "
            "-- the writer is Tier-1-root-only"
        ) from exc

    _ensure_private_dir(root)
    index_dir = root / "_index"
    _ensure_private_dir(index_dir)
    index_path = index_dir / report_id

    _create_index_entry(index_path, report_id, final_path)

    try:
        _ensure_private_dir(final_path.parent)
        tmp_path = root / f".{report_id}.tmp"
        _write_file_durable(tmp_path, json.dumps(report, indent=2, sort_keys=True))
        try:
            os.link(tmp_path, final_path)
        except FileExistsError as exc:
            raise LocalDiscoveryDuplicateReport(
                f"report_path {final_path} already exists -- refusing to overwrite an "
                "existing report"
            ) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        _fsync_dir(final_path.parent)
    except Exception:
        _update_index_status(index_dir, index_path, report_id, final_path, "failed")
        raise

    _update_index_status(index_dir, index_path, report_id, final_path, "written")
    return final_path
