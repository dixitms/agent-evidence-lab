"""Claude Code JSONL -> canonical v0.1 events.

Reads a snapshot of one `~/.claude/projects/<cwd-hash>/<session>.jsonl` and
emits canonical events plus one `experiment_run` record. Every rule here is
fixed by the v0.1 schemas; this module implements them rather than deciding
them.

Tier 1 only. Nothing in this module writes outside the local store, and it
deliberately contains no export path — the fail-closed public export
validator in `export_validator.py` is the only way out, and must not be
back-doored.
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
SOURCE_CLIENT = "claude-code"

# Fixed, documented namespace for deterministic event ids. Equal to
# uuid5(NAMESPACE_DNS, "agent-evidence-lab.local") so it is reproducible rather
# than a magic constant.
EVENT_ID_NAMESPACE = uuid.UUID("3924bc50-9bd4-50fa-a405-54d22c375082")

# Raw record dispositions, in code. An unrecognized
# `type` is a hard failure, never a silent default: the entire point of the
# table is that a new client event class surfaces instead of vanishing.
NORMALIZED = "normalized"
PRESERVED = "preserved-local-not-normalized"
UNSUPPORTED = "unsupported"

RAW_TYPE_DISPOSITION = {
    "user": NORMALIZED,
    "assistant": NORMALIZED,
    "system": NORMALIZED,  # only subtype == compact_boundary; others reclassified below
    "file-history-snapshot": PRESERVED,
    "file-history-delta": PRESERVED,
    "attachment": PRESERVED,
    "agent-name": PRESERVED,
    "fork-context-ref": PRESERVED,
    "ai-title": UNSUPPORTED,
    "last-prompt": UNSUPPORTED,
    "mode": UNSUPPORTED,
    "permission-mode": UNSUPPORTED,
    "queue-operation": UNSUPPORTED,
    "frame-link": UNSUPPORTED,
}

PROMPT_SOURCES = {"typed", "suggestion_accepted", "system", "sdk"}
SYNTHETIC_MODEL = "<synthetic>"


class UnrecognizedRawType(Exception):
    """A raw record type with no recorded disposition.

    Raised rather than defaulted. The working agreement is explicit:
    when a client's shape turns out different from what was recorded, fix the
    capability matrix — do not quietly work around it in code.
    """


@dataclass
class AdapterResult:
    events: list[dict[str, Any]]
    run: dict[str, Any]
    # See adapter_codex.reconcile: any reconciliation must verify this against
    # the bytes it counts. A live session file was observed growing 898 ->
    # 1,074 lines while being read, so this adapter faces the same hazard.
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
    """SHA-256 over RFC 8785 (JCS) canonical JSON — a named portable standard."""
    return hashlib.sha256(rfc8785.dumps(tool_input)).hexdigest()


def _text_bytes(content: Any) -> tuple[int | None, str]:
    """UTF-8 byte length of a tool result's text, after documented extraction.

    Returns (value, field_status). Claude Code emits `content` as a bare string
    or an array of typed blocks; both must reduce to the same unit or the
    metric is apples-to-oranges. Non-text blocks contribute 0 bytes and force
    'inferred', since their real payload size is not represented.
    """
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
    """Normalize to a repo-relative path; refuse anything outside the repo.

    `repo_root=None` (the explicit no-usable-root mode) short-circuits
    before any resolution is attempted -- mechanically guaranteed unavailable,
    not "usually fails."""
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


def normalize_session(
    source: "str | Path | SessionSnapshot",
    *,
    run_id: str,
    repo_root: str | Path | None,
    local_secret: bytes,
    repo_ref: str,
    run_kind: str = "private_dogfood",
    condition: str = "baseline",
    task_id: str = "dogfood-ingest",
    pinned_commit: str = "unknown",
    repo_url: str = "local-private",
) -> AdapterResult:
    """`repo_root=None` is an explicit "no usable root" mode: every
    `target_path` normalizes to `(None, "unavailable")` without attempting
    path resolution, rather than a caller guessing a directory that might
    coincidentally resolve some paths and not others. Existing callers keep
    passing a real root and are unaffected."""
    snapshot = SessionSnapshot.coerce(source)
    path = snapshot.source_path
    repo_root = Path(repo_root) if repo_root is not None else None
    raw_lines = snapshot.lines()

    events: list[dict[str, Any]] = []
    disposition_counts: dict[str, int] = {NORMALIZED: 0, PRESERVED: 0, UNSUPPORTED: 0}
    # Distinct reporting within `preserved`: a
    # promptId-only envelope must be visible as its own class, not merged
    # into a single opaque "preserved" bucket.
    preserved_detail: dict[str, int] = {}
    raw_type_counts: dict[str, int] = {}
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    models: set[str] = set()
    notes: list[str] = []
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

    # session_start — synthesized: Claude Code emits no native boundary record.
    # Notional line 0 keeps event_id within its documented four-tuple while
    # guaranteeing no collision with a real line.
    emit(
        event_id=_event_id(session_id or "", 0, None),
        event_type="session_start",
        event_origin="synthesized",
        synthesis_reason=(
            "Claude Code emits no native session-start record; synthesized from "
            "the first raw line's session identity and timestamp."
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
                f"disposition. Refusing to guess — add it to the "
                f"table (normalized / {PRESERVED} / {UNSUPPORTED}) with a reason."
            )

        disposition = RAW_TYPE_DISPOSITION[rtype]
        if rtype == "system" and rec.get("subtype") != "compact_boundary":
            disposition = PRESERVED  # other subtypes are client-runtime telemetry
        disposition_counts[disposition] += 1
        if disposition != NORMALIZED:
            continue

        ts = rec.get("timestamp")

        if rtype == "system":  # compact_boundary
            md = rec.get("compactMetadata", {})
            has_cum = "cumulativeDroppedTokens" in md
            emit(
                event_id=_event_id(session_id or "", line_no, None),
                event_type="compaction",
                event_origin="observed",
                synthesis_reason=None,
                timestamp=ts,
                raw_source_ref=raw_ref(line_no, None, raw),
                field_status={
                    "compaction_source": "observed",
                    "trigger": "observed",
                    "pre_tokens": "observed",
                    "post_tokens": "observed",
                    "cumulative_dropped_tokens": "observed" if has_cum else "unavailable",
                    "duration_ms": "observed",
                },
                compaction={
                    "compaction_source": "boundary_marker",
                    "trigger": md.get("trigger"),
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
                # A genuine agent input cycle. The boundary is a RECOGNIZED
                # promptSource — not `promptId is not None`, which
                # real-session reconciliation proved counts tool-result
                # carriers as turns (9,965 of them carry a promptId).
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
                # Retained locally, reported distinctly, but NOT an input cycle
                # in v0.1. Two classes land here:
                #   - isMeta harness injections (74 in the surveyed corpus)
                #   - promptId-only envelopes with no recognized promptSource
                #     (431): interruption markers, slash-command envelopes, and
                #     older-client prompts.
                # None emits a user_message, none increments turn_index, and
                # none is smoothed into prompt_source: null. If a controlled
                # fixture later shows a specific class is a meaningful agent
                # interaction, it gets modelled explicitly in v0.2 rather than
                # retrofitted into "turn".
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

            # Tool-result carrier. Reached regardless of whether the record
            # carries a promptId — gating this on promptId absence silently
            # dropped 391 of 494 results in the first spot-checked session.
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

        # rtype == "assistant"
        msg = rec.get("message", {})
        model = msg.get("model")
        if model and model != SYNTHETIC_MODEL:
            models.add(model)
        usage = msg.get("usage", {}) or {}

        # EXACTLY ONE envelope per assistant response. Its usage describes
        # the whole response, so json_pointer is null — attaching it per block
        # would multiply-count the response's tokens.
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
                emit(
                    event_id=_event_id(session_id or "", line_no, pointer),
                    event_type="thinking",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, pointer, raw),
                    field_status={"length_value": "observed"},
                    thinking={"length_value": len(block.get("thinking", "").encode("utf-8"))},
                )
            elif btype == "tool_use":
                tuid = block.get("id")
                calls[tuid] = calls.get(tuid, 0) + 1
                tool_input = block.get("input", {}) or {}
                target, target_status = _rel_path(tool_input.get("file_path"), repo_root)
                lr, lr_status = _line_range(tool_input)
                emit(
                    event_id=_event_id(session_id or "", line_no, pointer),
                    event_type="tool_call",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=raw_ref(line_no, pointer, raw),
                    field_status={
                        "tool_name": "observed",
                        "tool_correlation_id": "inferred",
                        "target_path": target_status,
                        "args_digest": "observed",
                        "line_range": lr_status,
                    },
                    tool_call={
                        "tool_name": block.get("name"),
                        "tool_correlation_id": _correlation_id(
                            local_secret, session_id or "", tuid
                        ),
                        "target_path": target,
                        "args_digest": _args_digest(tool_input),
                        "line_range": lr,
                    },
                )
            # 'text' blocks fold into the envelope event above — by design.

    emit(
        event_id=_event_id(session_id or "", len(raw_lines) + 1, None),
        event_type="session_end",
        event_origin="synthesized",
        synthesis_reason=(
            "No native session-end record exists and a session may be resumed "
            "later; synthesized from the last observed line at ingestion time."
        ),
        timestamp=parsed[-1][2].get("timestamp") if parsed else None,
        raw_source_ref=None,
        field_status={},
    )

    unmatched_calls = sorted(set(calls) - set(results))
    unmatched_results = sorted(set(results) - set(calls))

    # Model: observed from the trace, never <synthetic>. A run whose evidence
    # shows more than one model is scope-confounded, not silently averaged.
    model_value = next(iter(models)) if len(models) == 1 else None
    consistent = len(models) <= 1
    if len(models) > 1:
        notes.append(f"multiple models observed: {sorted(models)} -> scope_confounded")

    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": run_kind,
        "condition": condition,
        "policy_id": None,
        "policy_version": None,
        "model": model_value,
        "model_source": "observed_from_trace" if model_value else None,
        "model_evidence_consistent": consistent,
        "task_id": task_id,
        "pinned_commit": pinned_commit,
        "repo_url": repo_url,
        "disposition": "completed_analyzable" if consistent else "scope_confounded",
    }
    if not consistent:
        run["disposition_reason"] = "Multiple distinct models observed in agent-turn evidence."

    return AdapterResult(
        events=events,
        run=run,
        snapshot_sha256=snapshot.sha256,
        disposition_counts=disposition_counts,
        preserved_detail=preserved_detail,
        raw_type_counts=raw_type_counts,
        unmatched_calls=unmatched_calls,
        unmatched_results=unmatched_results,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Per-class reconciliation. Ground truth is recomputed from the raw snapshot by
# logic independent of normalize_session's output — the point is not to ask the
# adapter to confirm itself. Deliberately per-class: an aggregate ceiling passes
# while classes are wrong in compensating directions, and a coarse check is how
# 391 of 494 tool results went missing here in the first place.
# --------------------------------------------------------------------------

RECONCILED_CLASSES = (
    "tool_call", "tool_result", "thinking",
    "user_message", "assistant_message", "compaction",
)


def raw_class_counts(source: "str | Path | SessionSnapshot") -> dict[str, int]:
    """Count each authoritative raw class straight from the snapshot bytes."""
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
            c["assistant_message"] += 1  # exactly one envelope per response
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
    source: "str | Path | SessionSnapshot", result: AdapterResult
) -> dict[str, dict[str, Any]]:
    """Per-class reconciliation report: {class: {emitted, raw, ok}}.

    Raises SnapshotMismatch if `source` is not byte-identical to what was
    normalized. Claude Code's own session file grows while in use, so a path
    that has moved on fails loudly rather than being silently compared.
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


def first_user_prompt(source: "str | Path | SessionSnapshot") -> str | None:
    """The text actually delivered to the agent as its opening instruction.

    Used by the prompt-delivery check. Reads the raw transcript rather
    than canonical events, because canonical events deliberately carry no
    message text — the check has to happen at ingestion, against Tier 1.
    """
    for line in SessionSnapshot.coerce(source).lines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") != "user" or rec.get("promptSource") not in PROMPT_SOURCES:
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") in ("text", "input_text")
            )
    return None
