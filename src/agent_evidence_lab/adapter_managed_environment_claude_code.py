"""Managed-environment Claude Code trace-profile adapter (provisional, fixture-only).

**Status: provisional reference adapter.** Built entirely from
the managed-environment trace profile contract and its gap probe
specification. The gap probe
has run a **preliminary, four-session live sample** — that sample is not a
closed or representative answer to any of the four gaps it targets, and the
archived cohort remains unresolved (a snapshot mismatch during that probe
means archive was never actually inspected). Nothing in this module or its
tests has read any of that sample, or any other real managed environment trace — every
raw shape exercised here is hand-invented, following the same discipline as
`fixtures/raw/README.md`. Treat every "still open" note below as still open;
the preliminary sample narrows what's *unknown*, it does not close a gap.

This is **not** a new `source_client`. Per the trace-profile contract, a managed environment
deployment of Claude Code is a trace/deployment profile of the existing
`claude-code` client, not a fifth client — `SOURCE_CLIENT` below is the
same string `adapter_claude_code.py` emits, unchanged.

Deliberately a separate module from `adapter_claude_code.py`, mirroring the
existing precedent that `adapter_codex.py` does not import the Claude Code
adapter: two independently-reviewed contracts (personal-workstation Claude
Code vs. this provisional managed environment profile) should not become coupled
through shared code that only one of them has actually been reviewed to
carry. Where the raw JSONL shape is genuinely identical (tool call/result
pairing, `is_error` tri-state, `compact_boundary`/`isCompactSummary`
compaction, the `promptSource`-based turn rule), this module reimplements
it rather than importing it, so a future change to one adapter cannot
silently alter the other's already-reviewed behaviour.

Six defaults distinguish this module from `adapter_claude_code.py`. The
first three come from the trace-profile contract's privacy rules; the last three are
**explicit profile rules for this managed environment trace profile**, reviewed and
approved as such — not incidental deltas, and not "inherited behaviour"
from anywhere, since the personal adapter does none of them:

1. **Tool names and compaction trigger labels are `unavailable` unless
   separately allowlisted** (a trace-profile privacy rule). Claude Code's
   small public tool vocabulary is not assumed safe for a managed environment
   deployment by default.
2. **Thinking content is never measured.** `thinking.length_value` is
   always `null`/`unavailable` — this module does not read the `.thinking`
   text at all, unlike the personal adapter's UTF-8 byte-length metric
   (a trace-profile privacy rule; no separate justification exists yet).
3. **No per-message model identity is read, aggregated, or compared.**
   `run.model`, `model_source`, and `model_evidence_consistent` are always
   `null` — this module never calls anything equivalent to
   `msg.get("model")` (a trace-profile privacy rule).
4. **`mode` and `permission-mode` raw records are `preserved`, not
   `unsupported`, as an explicit profile rule.** The personal adapter
   classifies them `unsupported`; this profile retains them as a distinct,
   visible class instead — a deliberate managed environment-profile decision, and
   never treated as a session or turn boundary either way.
5. **`attachment`, `file-history-snapshot`, and `file-history-delta` are
   `unsupported` here, not `preserved` as in the personal adapter** — also
   an explicit profile rule, in the opposite direction from point 4: these
   can carry binary/file/diff content, which this profile's privacy review
   classified out of scope entirely, not safe to retain even as a
   content-free structural count.
6. **Every `system` raw record's `subtype` is individually allowlisted in
   `SYSTEM_SUBTYPE_DISPOSITION`, and an unreviewed subtype fails closed**
   (`UnrecognizedRawType`), rather than falling into one undifferentiated
   `preserved` bucket the way the personal adapter treats "anything but
   `compact_boundary`." The personal adapter documents five known
   subtypes (`turn_duration`, `api_error`, `away_summary`, `local_command`,
   `informational`) as homogeneous "client-runtime telemetry" — this
   profile's review found that grouping too coarse for a managed environment
   deployment: `turn_duration` is reviewed safe (numeric only) and stays
   `preserved`; `away_summary` and `local_command` can carry free-text
   content (`local_command` carries the actual raw command text — see
   elsewhere) and are `unsupported`; `api_error` and
   `informational` are deliberately **not** in the table at all, since
   nobody has yet assessed their content risk — they fail closed rather
   than being guessed into either bucket.

`parentUuid` is not read anywhere in this module, at all — the same
posture as `adapter_claude_code.py`, restated here because the contract's
The trace-profile contract makes it an explicit requirement rather than an
incidental omission: a
malformed `parentUuid` graph (multiple roots, cycles, orphan references)
cannot affect this adapter's output, because the field never reaches any
code path that could use it for ordering or turn reconstruction.

Only one trace profile is implemented: `"managed-environment-live-provisional"`.
Requesting any other profile (in particular an archived-cohort profile)
raises `TraceProfileUnavailable` immediately, before a single byte is read
— per the contract's "archive support remains unavailable until a clean
archive probe closes it."

Every run this module produces is hardcoded `run_kind: "calibration"`,
`disposition: "excluded"` — never configurable by the caller. This is the
same structural pattern the calibration-corpus rule already uses, and it
is a real, code-enforced default: proven directly (not merely asserted) by
calling `export_validator.validate_export` against this module's own
fixture output, which raises `ExportRefused` citing `run_kind='calibration'`
explicitly. It also matches the codebase-wide convention that "only
`completed_analyzable` runs enter a compliance comparison"
(`experiment_run.schema.json`'s own `disposition` description) — any
downstream tool that already honors that convention skips this data.

**What this does NOT do** (round-2 correction, so this isn't overclaimed):
`trace_profile` exists only on this module's Python return value — it is
never written into an emitted event or into the `run` record. Nothing
technically stops a caller from taking `result.events`/`result.run`,
discarding the `ManagedEnvironmentAdapterResult` wrapper, and feeding them to a
detector or report that ignores `disposition`. "No detector, report, or
dashboard code in this repository reads this module's output" is therefore
a **scope fact about this change** (no such code exists here) plus a
**convention** downstream code is expected to honor via `disposition`/
`run_kind` — not a technical barrier this module enforces on its own. A
durable, schema-level profile-identity mechanism is an open contract gap,
still open; adding one here would resolve that governance question as a
side effect of an adapter change, which this module deliberately does not
do.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rfc8785

from .snapshot import SessionSnapshot

SCHEMA_VERSION = "0.1.1"
SOURCE_CLIENT = "claude-code"  # unchanged — see module docstring

# Same fixed namespace as adapter_claude_code.py — event_id's four-tuple
# is (source_client, session_id, line_or_offset, json_pointer), and
# source_client is unchanged here, so reusing the constant keeps ids within
# the one documented, reproducible scheme rather than forking it.
EVENT_ID_NAMESPACE = uuid.UUID("3924bc50-9bd4-50fa-a405-54d22c375082")

NORMALIZED = "normalized"
PRESERVED = "preserved-local-not-normalized"
UNSUPPORTED = "unsupported"

# Delta from adapter_claude_code.RAW_TYPE_DISPOSITION, per this profile's
# own privacy review (round-2 correction):
#   - "mode" / "permission-mode" are PRESERVED here as an explicit,
#     reviewed PROFILE RULE — not "inherited behaviour" from the personal
#     adapter, which classifies them UNSUPPORTED. The distinction matters:
#     this is a deliberate managed environment-profile decision, authorised by the
#     request that introduced it, not a side effect of copying the table.
#   - "attachment", "file-history-snapshot", "file-history-delta" are
#     UNSUPPORTED here, not PRESERVED as in the personal adapter: these can
#     carry binary/file/diff content, which the managed environment profile's privacy
#     review classified out-of-scope rather than safe to retain even as a
#     structural, content-free count.
RAW_TYPE_DISPOSITION = {
    "user": NORMALIZED,
    "assistant": NORMALIZED,
    "system": NORMALIZED,  # per-subtype disposition in SYSTEM_SUBTYPE_DISPOSITION below
    "file-history-snapshot": UNSUPPORTED,
    "file-history-delta": UNSUPPORTED,
    "attachment": UNSUPPORTED,
    "agent-name": PRESERVED,
    "fork-context-ref": PRESERVED,
    "mode": PRESERVED,
    "permission-mode": PRESERVED,
    "ai-title": UNSUPPORTED,
    "last-prompt": UNSUPPORTED,
    "queue-operation": UNSUPPORTED,
    "frame-link": UNSUPPORTED,
}

# Fail-closed per-subtype table for rtype == "system" (round-2 correction):
# the personal adapter treats every non-compact_boundary subtype as one
# undifferentiated PRESERVED bucket, which would let an unreviewed subtype
# pass through silently — exactly the raw-kind-inventory gap this profile
# is supposed to close. Every subtype this adapter will accept must be
# named here with a reviewed reason; anything else raises
# UnrecognizedRawType. Known subtypes per the raw-type table
# (`turn_duration`, `api_error`, `away_summary`, `local_command`,
# `informational`) are NOT all pre-approved just because they're already
# documented for the personal adapter — `api_error` and `informational` are
# deliberately left OUT of this table (not reviewed for managed environment content
# risk yet) so they fail closed rather than being guessed into PRESERVED.
SYSTEM_SUBTYPE_DISPOSITION = {
    "compact_boundary": NORMALIZED,   # handled specially below; carries no raw content
    "turn_duration": PRESERVED,       # numeric telemetry only, reviewed safe
    "away_summary": UNSUPPORTED,      # may carry free-text status content
    "local_command": UNSUPPORTED,     # carries the actual raw command text
}

PROMPT_SOURCES = {"typed", "suggestion_accepted", "system", "sdk"}

# Only the live cohort is implemented (an open contract gap / gap probe
# gap B are still open for the archived cohort's own field differences).
SUPPORTED_TRACE_PROFILES = frozenset({"managed-environment-live-provisional"})

PROVISIONAL_NOTE = (
    "provisional managed environment trace profile ('managed-environment-live-provisional'); "
    "Open trace-profile gaps 2/3/5/8 have not been "
    "closed against real managed environment data by a gap probe. "
    "This run is run_kind=calibration/disposition=excluded and must never be "
    "treated as evidence, fed to a detector or report, or used to support an "
    "organisational claim."
)


class UnrecognizedRawType(Exception):
    """A raw record type with no recorded disposition — raised, never defaulted.

    Same posture as `adapter_claude_code.UnrecognizedRawType`: a client shape
    this table doesn't know about must surface as a failure, not vanish.
    """


class TraceProfileUnavailable(Exception):
    """Requested a trace profile this provisional adapter does not implement.

    In particular: the archived cohort. Per the contract, archive support
    stays unavailable until a clean archive probe closes the gap-probe
    spec's open items for that cohort — this is not a temporary code gap to
    work around, it is the intended refusal.
    """


@dataclass
class ManagedEnvironmentAdapterResult:
    events: list[dict[str, Any]]
    run: dict[str, Any]
    trace_profile: str
    snapshot_sha256: str
    disposition_counts: dict[str, int]
    preserved_detail: dict[str, int]
    raw_type_counts: dict[str, int]
    unmatched_calls: list[str]
    unmatched_results: list[str]
    notes: list[str] = field(default_factory=list)


def _event_id(session_id: str, line: int | str, pointer: str | None) -> str:
    name = f"{SOURCE_CLIENT}|{session_id}|{line}|{pointer or ''}"
    return str(uuid.uuid5(EVENT_ID_NAMESPACE, name))


def _correlation_id(local_secret: bytes, session_id: str, raw_tool_use_id: str) -> str:
    msg = f"{SOURCE_CLIENT}{session_id}{raw_tool_use_id}".encode()
    return hmac.new(local_secret, msg, hashlib.sha256).hexdigest()


def _args_digest(tool_input: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(tool_input)).hexdigest()


def _text_bytes(content: Any) -> tuple[int | None, str]:
    if content is None:
        return None, "unavailable"
    if isinstance(content, str):
        return len(content.encode("utf-8")), "observed"
    if isinstance(content, list):
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        has_non_text = any(b.get("type") != "text" for b in content)
        return len(text.encode("utf-8")), "inferred" if has_non_text else "observed"
    return None, "unavailable"


def _rel_path(raw_path: str | None, repo_root: Path | None) -> tuple[str | None, str]:
    if not raw_path or repo_root is None:
        return None, "unavailable"
    try:
        resolved = Path(raw_path).resolve()
        rel = resolved.relative_to(repo_root.resolve())
    except (ValueError, OSError):
        return None, "unavailable"
    return str(rel), "observed"


def _line_range(tool_input: dict[str, Any]) -> tuple[dict[str, int] | None, str]:
    offset, limit = tool_input.get("offset"), tool_input.get("limit")
    if isinstance(offset, int) and isinstance(limit, int):
        return {"start": offset, "end": offset + limit - 1}, "inferred"
    return None, "unavailable"


def normalize_managed_environment_session(
    source: "str | Path | SessionSnapshot",
    *,
    run_id: str,
    local_secret: bytes,
    repo_ref: str,
    trace_profile: str = "managed-environment-live-provisional",
    tool_name_allowlist: frozenset[str] = frozenset(),
    trigger_label_allowlist: frozenset[str] = frozenset(),
    repo_root: str | Path | None = None,
    task_id: str = "managed-environment-trace-profile-provisional",
    pinned_commit: str = "unknown",
    repo_url: str = "managed-environment-local-private",
) -> ManagedEnvironmentAdapterResult:
    """Normalize one managed environment `claude-code` session under the provisional profile.

    `repo_root=None` by default (unlike the personal adapter, which expects a
    caller-supplied root): `target_path` is `unavailable` unless a caller
    explicitly opts in with a real root, which matches the conservative
    default this profile applies to every other unreviewed field.

    `repo_ref` must already be a pre-approved opaque label (a trace-profile privacy rule —
    "no native safe opaque trace field... a future adapter may accept an
    externally supplied, managed environment-local opaque project label from an
    explicit manifest"). This function does not derive one from `cwd` or
    `gitBranch`, and never reads either field from the raw record.

    `run_kind`/`condition`/`disposition`/`policy_id`/`policy_version` are not
    parameters: they are fixed below, not because a caller couldn't want
    something else, but because this profile has exactly one legitimate
    shape (calibration, excluded, no policy under test) and making that
    configurable would let a caller quietly turn a provisional trace profile
    into something that looks like evidence.
    """
    if trace_profile not in SUPPORTED_TRACE_PROFILES:
        raise TraceProfileUnavailable(
            f"trace_profile {trace_profile!r} is not implemented by this "
            f"provisional adapter. Only {sorted(SUPPORTED_TRACE_PROFILES)} is "
            f"supported — archive-cohort support remains unavailable until a "
            f"clean archive probe closes the gap probe's "
            f"open items for that cohort."
        )

    snapshot = SessionSnapshot.coerce(source)
    path = snapshot.source_path
    repo_root = Path(repo_root) if repo_root is not None else None
    raw_lines = snapshot.lines()

    events: list[dict[str, Any]] = []
    disposition_counts: dict[str, int] = {NORMALIZED: 0, PRESERVED: 0, UNSUPPORTED: 0}
    preserved_detail: dict[str, int] = {}
    raw_type_counts: dict[str, int] = {}
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    notes: list[str] = [PROVISIONAL_NOTE]
    session_id: str | None = None
    client_version: str | None = None
    turn = -1
    seq = 0

    def emit(**ev: Any) -> None:
        nonlocal seq
        ev["schema_version"] = SCHEMA_VERSION
        ev["sequence_index"] = seq
        ev["run_id"] = run_id
        ev["source_client"] = SOURCE_CLIENT
        ev["source_client_version"] = client_version
        ev["session_id"] = session_id
        ev["repo_ref"] = repo_ref
        ev["turn_index"] = max(turn, 0)
        events.append(ev)
        seq += 1

    def raw_ref(line_no: int, pointer: str | None, raw: str) -> dict[str, Any]:
        return {
            "local_path": str(path),
            "line_or_offset": line_no,
            "json_pointer": pointer,
            "content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }

    parsed: list[tuple[int, str, dict[str, Any]]] = []
    for i, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        rec = json.loads(raw)
        parsed.append((i, raw, rec))
        session_id = session_id or rec.get("sessionId")
        client_version = client_version or rec.get("version")

    # session_start — synthesized, same posture as adapter_claude_code.py:
    # neither cohort is confirmed to carry a native boundary marker
    # (gap probe item B, part 4, still open), so the
    # conservative default is to keep synthesizing rather than assume one.
    emit(
        event_id=_event_id(session_id or "", 0, None),
        event_type="session_start",
        event_origin="synthesized",
        synthesis_reason=(
            "No native session-start record is confirmed for this trace "
            "profile (gap-probe item still open); synthesized from the "
            "first raw line's session identity and timestamp, same as the "
            "personal Claude Code adapter's posture."
        ),
        timestamp=parsed[0][2].get("timestamp") if parsed else None,
        raw_source_ref=None,
        field_status={},
    )

    for line_no, raw, rec in parsed:
        rtype = rec.get("type")
        raw_type_counts[rtype] = raw_type_counts.get(rtype, 0) + 1

        if rtype not in RAW_TYPE_DISPOSITION:
            raise UnrecognizedRawType(
                f"{path}:{line_no} has raw type {rtype!r}, which has no recorded "
                f"disposition. Refusing to guess — add it to the table "
                f"(normalized / {PRESERVED} / {UNSUPPORTED}) with a reason."
            )

        disposition = RAW_TYPE_DISPOSITION[rtype]
        if rtype == "system":
            subtype = rec.get("subtype")
            if subtype not in SYSTEM_SUBTYPE_DISPOSITION:
                raise UnrecognizedRawType(
                    f"{path}:{line_no} has system subtype {subtype!r}, which has no "
                    f"reviewed disposition for this managed environment trace profile. Refusing "
                    f"to guess — add it to SYSTEM_SUBTYPE_DISPOSITION with a reviewed "
                    f"reason (content-risk assessed, not merely copied from the "
                    f"personal adapter's grouping)."
                )
            disposition = SYSTEM_SUBTYPE_DISPOSITION[subtype]
        disposition_counts[disposition] += 1
        if disposition != NORMALIZED:
            continue

        ts = rec.get("timestamp")

        if rtype == "system":  # compact_boundary
            md = rec.get("compactMetadata", {})
            has_cum = "cumulativeDroppedTokens" in md
            raw_trigger = md.get("trigger")
            trigger_ok = raw_trigger in trigger_label_allowlist
            emit(
                event_id=_event_id(session_id or "", line_no, None),
                event_type="compaction",
                event_origin="observed",
                synthesis_reason=None,
                timestamp=ts,
                raw_source_ref=raw_ref(line_no, None, raw),
                field_status={
                    "compaction_source": "observed",
                    "trigger": "observed" if trigger_ok else "unavailable",
                    "pre_tokens": "observed",
                    "post_tokens": "observed",
                    "cumulative_dropped_tokens": "observed" if has_cum else "unavailable",
                    "duration_ms": "observed",
                },
                compaction={
                    "compaction_source": "boundary_marker",
                    "trigger": raw_trigger if trigger_ok else None,
                    "pre_tokens": md.get("preTokens"),
                    "post_tokens": md.get("postTokens"),
                    "cumulative_dropped_tokens": md.get("cumulativeDroppedTokens"),
                    "duration_ms": md.get("durationMs"),
                },
            )
            continue

        if rtype == "user":
            if rec.get("isCompactSummary"):
                emit(
                    event_id=_event_id(session_id or "", line_no, None),
                    event_type="compaction",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, None, raw),
                    field_status={
                        "compaction_source": "observed",
                        "trigger": "unavailable",
                        "pre_tokens": "unavailable",
                        "post_tokens": "unavailable",
                        "cumulative_dropped_tokens": "unavailable",
                        "duration_ms": "unavailable",
                    },
                    compaction={
                        "compaction_source": "continuation_summary",
                        "trigger": None,
                        "pre_tokens": None,
                        "post_tokens": None,
                        "cumulative_dropped_tokens": None,
                        "duration_ms": None,
                    },
                )
                continue

            blocks = (rec.get("message", {}) or {}).get("content") or []
            tool_results = (
                [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                if isinstance(blocks, list)
                else []
            )

            src = rec.get("promptSource")
            if src is not None and src not in PROMPT_SOURCES:
                raise ValueError(
                    f"{path}:{line_no} has unrecognized promptSource {src!r}. "
                    f"Failing rather than coercing to null."
                )

            if not tool_results and not rec.get("isMeta") and src in PROMPT_SOURCES:
                # promptSource is optional on a user record (per the requested
                # coverage): when present AND recognized, it opens a turn,
                # same rule as the personal adapter's already-reviewed 0.1.1
                # boundary — this profile does not change turn semantics,
                # since trace-profile contract item 3
                # leaves that question open rather than resolved differently.
                turn += 1
                emit(
                    event_id=_event_id(session_id or "", line_no, None),
                    event_type="user_message",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, None, raw),
                    field_status={"prompt_source": "observed"},
                    user_message={"prompt_source": src},
                )
                continue

            if not tool_results:
                # promptSource absent (or unrecognized-but-null) is retained
                # locally and reported distinctly, never smoothed into a
                # turn or into prompt_source: null — same posture as the
                # personal adapter, and the conservative default while
                # gap probe item B part 3 (whether
                # promptSource is ever absent from an apparent user message)
                # remains open.
                reason = (
                    "meta_injection"
                    if rec.get("isMeta")
                    else "prompt_id_only_envelope"
                    if rec.get("promptId") is not None
                    else "user_record_without_prompt_source"
                )
                disposition_counts[NORMALIZED] -= 1
                disposition_counts[PRESERVED] += 1
                preserved_detail[reason] = preserved_detail.get(reason, 0) + 1
                continue

            for idx, block in enumerate(blocks):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tuid = block.get("tool_use_id")
                results[tuid] = results.get(tuid, 0) + 1
                size, size_status = _text_bytes(block.get("content"))
                is_error = block.get("is_error")
                emit(
                    event_id=_event_id(session_id or "", line_no, f"/message/content/{idx}"),
                    event_type="tool_result",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, f"/message/content/{idx}", raw),
                    field_status={
                        "tool_correlation_id": "inferred",
                        "success": "observed" if is_error is not None else "unavailable",
                        "result_size_value": size_status,
                        "result_size_method": "observed",
                    },
                    tool_result={
                        "tool_correlation_id": _correlation_id(
                            local_secret, session_id or "", tuid
                        ),
                        "success": (not is_error) if is_error is not None else None,
                        "result_size_value": size,
                        "result_size_unit": "utf8_bytes",
                        "result_size_method": "direct" if size is not None else "unavailable",
                    },
                )
            continue

        # rtype == "assistant" — model identity is deliberately never read
        # here (module docstring point 3): no `msg.get("model")`, no model
        # set, no consistency check, no scope_confounded from model evidence.
        msg = rec.get("message", {})
        usage = msg.get("usage", {}) or {}

        emit(
            event_id=_event_id(session_id or "", line_no, None),
            event_type="assistant_message",
            event_origin="observed",
            synthesis_reason=None,
            timestamp=ts,
            raw_source_ref=raw_ref(line_no, None, raw),
            field_status={
                k: ("observed" if k_src in usage else "unavailable")
                for k, k_src in [
                    ("input_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("cache_read_input_tokens", "cache_read_input_tokens"),
                    ("cache_creation_input_tokens", "cache_creation_input_tokens"),
                ]
            },
            assistant_message={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            },
        )

        for idx, block in enumerate(msg.get("content", []) or []):
            btype = block.get("type")
            pointer = f"/message/content/{idx}"
            if btype == "thinking":
                # Content-derived length is never computed here (module
                # docstring point 2) — unlike adapter_claude_code.py, this
                # branch does not read block.get("thinking") at all.
                emit(
                    event_id=_event_id(session_id or "", line_no, pointer),
                    event_type="thinking",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, pointer, raw),
                    field_status={"length_value": "unavailable"},
                    thinking={"length_value": None},
                )
            elif btype == "tool_use":
                tuid = block.get("id")
                calls[tuid] = calls.get(tuid, 0) + 1
                tool_input = block.get("input", {}) or {}
                target, target_status = _rel_path(tool_input.get("file_path"), repo_root)
                lr, lr_status = _line_range(tool_input)
                raw_name = block.get("name")
                name_ok = raw_name in tool_name_allowlist

                tool_call_payload: dict[str, Any] = {
                    "tool_correlation_id": _correlation_id(
                        local_secret, session_id or "", tuid
                    ),
                    "target_path": target,
                    "args_digest": _args_digest(tool_input),
                    "line_range": lr,
                }
                status: dict[str, str] = {
                    "tool_correlation_id": "inferred",
                    "target_path": target_status,
                    "args_digest": "observed",
                    "line_range": lr_status,
                }
                # tool_name's schema type is plain "string" (not nullable):
                # unlike target_path/trigger, "unavailable" cannot be
                # represented as a null value in the field. The only honest
                # representation is to omit the key from both the payload
                # and field_status entirely — the schema's own field_status
                # rule ("a field absent here MUST NOT appear elsewhere")
                # already requires exactly this pairing.
                if name_ok:
                    tool_call_payload["tool_name"] = raw_name
                    status["tool_name"] = "observed"

                emit(
                    event_id=_event_id(session_id or "", line_no, pointer),
                    event_type="tool_call",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, pointer, raw),
                    field_status=status,
                    tool_call=tool_call_payload,
                )
            # 'text' blocks fold into the envelope event above — by design.

    emit(
        event_id=_event_id(session_id or "", len(raw_lines) + 1, None),
        event_type="session_end",
        event_origin="synthesized",
        synthesis_reason=(
            "No native session-end record is confirmed for this trace "
            "profile; synthesized from the last observed line at ingestion "
            "time, same posture as session_start above."
        ),
        timestamp=parsed[-1][2].get("timestamp") if parsed else None,
        raw_source_ref=None,
        field_status={},
    )

    unmatched_calls = sorted(set(calls) - set(results))
    unmatched_results = sorted(set(results) - set(calls))

    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": "calibration",
        "condition": "baseline",
        "policy_id": None,
        "policy_version": None,
        "model": None,
        "model_source": None,
        "model_evidence_consistent": None,
        "task_id": task_id,
        "pinned_commit": pinned_commit,
        "repo_url": repo_url,
        "disposition": "excluded",
        "disposition_reason": PROVISIONAL_NOTE,
    }

    return ManagedEnvironmentAdapterResult(
        events=events,
        run=run,
        trace_profile=trace_profile,
        snapshot_sha256=snapshot.sha256,
        disposition_counts=disposition_counts,
        preserved_detail=preserved_detail,
        raw_type_counts=raw_type_counts,
        unmatched_calls=unmatched_calls,
        unmatched_results=unmatched_results,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Per-class reconciliation — same six classes and same "ground truth
# recomputed independently from the raw snapshot" discipline as
# adapter_claude_code.reconcile. Duplicated rather than imported for the
# same reason as the rest of this module (see the module docstring): the
# counting logic below depends only on raw structural shape, never on the
# managed environment suppression policy, but keeping it local means a future change
# to the personal adapter's reconciliation cannot silently change this
# profile's gate.
# --------------------------------------------------------------------------

RECONCILED_CLASSES = (
    "tool_call", "tool_result", "thinking",
    "user_message", "assistant_message", "compaction",
)


def raw_class_counts(source: "str | Path | SessionSnapshot") -> dict[str, int]:
    c = {k: 0 for k in RECONCILED_CLASSES}
    c.update(
        turns=0,
        prompt_id_only_envelope=0,
        user_record_without_prompt_source=0,
        meta_injection=0,
    )

    for line in SessionSnapshot.coerce(source).lines():
        if not line.strip():
            continue
        rec = json.loads(line)
        rtype = rec.get("type")

        if rtype == "assistant":
            c["assistant_message"] += 1
            for block in (rec.get("message", {}) or {}).get("content", []) or []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "thinking":
                    c["thinking"] += 1
                elif btype == "tool_use":
                    c["tool_call"] += 1

        elif rtype == "user":
            if rec.get("isCompactSummary"):
                c["compaction"] += 1
                continue
            blocks = (rec.get("message", {}) or {}).get("content") or []
            tool_results = (
                [b for b in blocks
                 if isinstance(b, dict) and b.get("type") == "tool_result"]
                if isinstance(blocks, list) else []
            )
            if tool_results:
                c["tool_result"] += len(tool_results)
            elif rec.get("isMeta"):
                c["meta_injection"] += 1
            elif rec.get("promptSource") in PROMPT_SOURCES:
                c["user_message"] += 1
                c["turns"] += 1
            elif rec.get("promptId") is not None:
                c["prompt_id_only_envelope"] += 1
            else:
                c["user_record_without_prompt_source"] += 1

        elif rtype == "system" and rec.get("subtype") == "compact_boundary":
            c["compaction"] += 1

    return c


def reconcile(
    source: "str | Path | SessionSnapshot", result: ManagedEnvironmentAdapterResult
) -> dict[str, dict[str, Any]]:
    """Per-class reconciliation report; raises SnapshotMismatch on drift.

    Identical contract to `adapter_claude_code.reconcile`: `source` must be
    byte-identical to what was normalized, verified via
    `SessionSnapshot.verify_same_as`, which raises `SnapshotMismatch` rather
    than silently comparing against different bytes.
    """
    snapshot = SessionSnapshot.coerce(source)
    snapshot.verify_same_as(result.snapshot_sha256)
    raw = raw_class_counts(snapshot)

    emitted: dict[str, int] = {}
    for ev in result.events:
        emitted[ev["event_type"]] = emitted.get(ev["event_type"], 0) + 1

    report: dict[str, dict[str, Any]] = {}
    for cls in RECONCILED_CLASSES:
        e, r = emitted.get(cls, 0), raw[cls]
        report[cls] = {"emitted": e, "raw": r, "ok": e == r}

    max_turn = max((ev["turn_index"] for ev in result.events), default=0)
    turns_emitted = max_turn + 1 if raw["turns"] else 0
    report["turns"] = {
        "emitted": turns_emitted, "raw": raw["turns"], "ok": turns_emitted == raw["turns"]}

    report["_preserved"] = {
        "prompt_id_only_envelope_raw": raw["prompt_id_only_envelope"],
        "prompt_id_only_envelope_preserved":
            result.preserved_detail.get("prompt_id_only_envelope", 0),
        "user_record_without_prompt_source_raw": raw["user_record_without_prompt_source"],
        "user_record_without_prompt_source_preserved": result.preserved_detail.get(
            "user_record_without_prompt_source", 0
        ),
        "meta_injection_raw": raw["meta_injection"],
        "meta_injection_preserved": result.preserved_detail.get("meta_injection", 0),
        "ok": (
            raw["prompt_id_only_envelope"]
            == result.preserved_detail.get("prompt_id_only_envelope", 0)
            and raw["user_record_without_prompt_source"]
            == result.preserved_detail.get("user_record_without_prompt_source", 0)
            and raw["meta_injection"] == result.preserved_detail.get("meta_injection", 0)
        ),
    }
    return report
