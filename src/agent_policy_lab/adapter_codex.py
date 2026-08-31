"""Codex CLI rollout JSONL -> canonical v0.2.0 events.

Reads one `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` snapshot and
emits canonical events plus one `experiment_run` record.

Authority is assigned **per canonical event class**, not per stream — the
rollout file carries two parallel streams with disjoint id spaces for tool
activity, so no single stream satisfies the contract:

  * `response_item`  -> user_message, assistant_message, tool_call,
                        tool_result, thinking
  * `compacted`      -> compaction
  * `task_started`   -> turn_index
  * `item_completed` -> PRESERVED AND REPORTED ONLY. Never normalized: its ids
                        are disjoint from `response_item`'s, so emitting events
                        from it would double-count the same work.

`schema_version` 0.2.0 is frozen from this module's first emission. Compaction
structure may be characterized during calibration, but a richer representation
belongs in a later version, not a quiet 0.2 change.

Tier 1 only. No export path lives here, by design.
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

SCHEMA_VERSION = "0.2.0"
SOURCE_CLIENT = "codex-cli"
EVENT_ID_NAMESPACE = uuid.UUID("64f66254-faf2-5c58-9026-b9af6abf735c")

NORMALIZED = "normalized"
PRESERVED = "preserved-local-not-normalized"
UNSUPPORTED = "unsupported"

# Wrapper types. `response_item` and `compacted` are the only normalizing ones.
WRAPPER_DISPOSITION = {
    "session_meta": NORMALIZED,
    "response_item": NORMALIZED,
    "compacted": NORMALIZED,
    "event_msg": PRESERVED,     # per-subtype reasons below; task_started drives turns
    "turn_context": PRESERVED,
    "world_state": PRESERVED,
}

# Every event_msg subtype must be named. An unknown one fails loudly rather
# than defaulting — the disposition table exists so a new client event class
# surfaces as an error instead of vanishing.
EVENT_MSG_REASON = {
    "task_started": "turn_marker_consumed",
    "task_complete": "turn_marker_consumed",
    "item_completed": "semantic_projection_item_completed",
    "token_count": "cumulative_token_stream",
    "thread_settings_applied": "client_settings",
    "agent_message": "semantic_projection_message",
    "user_message": "semantic_projection_message",
    "agent_reasoning": "semantic_projection_reasoning",
    "web_search_end": "tool_activity_projection",
    "patch_apply_end": "tool_activity_projection",
    "mcp_tool_call_end": "tool_activity_projection",
    "turn_aborted": "turn_marker_consumed",
}

TOOL_CALL_TYPES = {"custom_tool_call", "function_call"}
TOOL_OUTPUT_TYPES = {"custom_tool_call_output", "function_call_output"}
CANONICAL_ROLES = {"user", "assistant"}


class UnrecognizedRawType(Exception):
    """A raw wrapper type or event_msg subtype with no recorded disposition."""


class MissingAnchorCommit(Exception):
    """Session has no `session_meta.git.commit_hash`.

    Excluded from calibration normalization rather than normalized with a
    placeholder: `experiment_run.pinned_commit` is required, and weakening the
    pinned-commit contract to admit a session is the wrong trade.
    """


@dataclass
class CodexAdapterResult:
    events: list[dict[str, Any]]
    run: dict[str, Any]
    # Hash of the exact bytes normalized. reconcile() refuses to compare
    # against anything else, so an append mid-run cannot pass unnoticed.
    snapshot_sha256: str
    disposition_counts: dict[str, int]
    preserved_detail: dict[str, int]
    wrapper_counts: dict[str, int]
    unmatched_calls: list[str]
    unmatched_results: list[str]
    notes: list[str] = field(default_factory=list)


def _event_id(session_id: str, line: int | str, pointer: str | None) -> str:
    return str(uuid.uuid5(
        EVENT_ID_NAMESPACE, f"{SOURCE_CLIENT}|{session_id}|{line}|{pointer or ''}"))


def _correlation_id(local_secret: bytes, session_id: str, raw_call_id: str) -> str:
    msg = f"{SOURCE_CLIENT}{session_id}{raw_call_id}".encode()
    return hmac.new(local_secret, msg, hashlib.sha256).hexdigest()


def _args_digest(tool_input: Any) -> tuple[str | None, str]:
    """SHA-256 over RFC 8785 (JCS) canonical JSON of the tool input.

    Codex's `custom_tool_call.input` is an opaque string and `function_call`
    carries none at all, so the digest is over whatever the client recorded.
    """
    if tool_input is None:
        return None, "unavailable"
    return hashlib.sha256(rfc8785.dumps(tool_input)).hexdigest(), "observed"


def _output_bytes(output: Any) -> tuple[int | None, str]:
    """UTF-8 byte length of a tool output's text, after documented extraction.

    Codex emits `output` as a bare string (250 of 2,335 corpus records) or a
    list of blocks (2,085). Both must reduce to the same unit or the metric is
    apples-to-oranges — same rule as the Claude Code adapter.
    """
    if output is None:
        return None, "unavailable"
    if isinstance(output, str):
        return len(output.encode("utf-8")), "observed"
    if isinstance(output, list):
        texts, non_text = [], False
        for b in output:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                texts.append(b["text"])
            else:
                non_text = True
        return len("".join(texts).encode("utf-8")), "inferred" if non_text else "observed"
    return None, "unavailable"


def _message_text_len(content: Any) -> int | None:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        return len("".join(
            b["text"] for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ).encode("utf-8"))
    return None


def normalize_session(
    source: "str | Path | SessionSnapshot",
    *,
    run_id: str,
    local_secret: bytes,
    repo_ref: str,
    run_kind: str = "calibration",
    condition: str = "baseline",
    task_id: str = "codex-calibration",
    disposition: str = "excluded",
    disposition_reason: str = (
        "Private calibration cohort — adapter calibration only, never evidence."
    ),
) -> CodexAdapterResult:
    snapshot = SessionSnapshot.coerce(source)
    path = snapshot.source_path
    raw_lines = snapshot.lines()

    parsed: list[tuple[int, str, dict[str, Any]]] = []
    for i, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        parsed.append((i, raw, json.loads(raw)))

    meta = next(
        ((ln, r.get("payload") or {}) for ln, _, r in parsed if r.get("type") == "session_meta"),
        None,
    )
    if meta is None:
        raise MissingAnchorCommit(f"{path}: no session_meta record; cannot anchor the session")
    meta_line, m = meta
    git = m.get("git") or {}
    commit = git.get("commit_hash")
    if not commit:
        raise MissingAnchorCommit(
            f"{path}: session_meta.git.commit_hash is absent. Excluded from calibration "
            f"normalization — pinned_commit is a required contract field and is not weakened "
            f"to admit a session."
        )

    session_id = str(m.get("session_id") or m.get("id") or "")
    client_version = m.get("cli_version")
    models: set[str] = set()

    events: list[dict[str, Any]] = []
    disposition_counts = {NORMALIZED: 0, PRESERVED: 0, UNSUPPORTED: 0}
    preserved_detail: dict[str, int] = {}
    wrapper_counts: dict[str, int] = {}
    calls: set[str] = set()
    results: set[str] = set()
    notes: list[str] = []
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

    def ref(line_no: int, raw: str) -> dict[str, Any]:
        return {
            "local_path": str(path),
            "line_or_offset": line_no,
            "json_pointer": None,  # one canonical event per raw line for Codex
            "content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }

    def preserve(reason: str) -> None:
        disposition_counts[NORMALIZED] -= 1
        disposition_counts[PRESERVED] += 1
        preserved_detail[reason] = preserved_detail.get(reason, 0) + 1

    emit(
        event_id=_event_id(session_id, 0, None),
        event_type="session_start",
        event_origin="observed",  # native session_meta — enforced by the v0.2 schema
        synthesis_reason=None,
        timestamp=m.get("timestamp") or parsed[0][2].get("timestamp"),
        raw_source_ref=ref(meta_line, parsed[meta_line - 1][1]),
        field_status={},
    )

    for line_no, raw, rec in parsed:
        wtype = rec.get("type")
        wrapper_counts[wtype] = wrapper_counts.get(wtype, 0) + 1

        if wtype not in WRAPPER_DISPOSITION:
            raise UnrecognizedRawType(
                f"{path}:{line_no} has wrapper type {wtype!r}, which has no recorded "
                f"disposition. Refusing to guess — add it to WRAPPER_DISPOSITION and to "
                f"the raw-type table with a reason."
            )

        disp = WRAPPER_DISPOSITION[wtype]
        disposition_counts[disp] += 1
        p = rec.get("payload") or {}
        ts = rec.get("timestamp")

        if wtype == "session_meta":
            continue  # already emitted as session_start above

        if wtype == "event_msg":
            sub = p.get("type")
            if sub not in EVENT_MSG_REASON:
                raise UnrecognizedRawType(
                    f"{path}:{line_no} has event_msg subtype {sub!r} with no recorded "
                    f"disposition. Refusing to default it into a bucket."
                )
            reason = EVENT_MSG_REASON[sub]
            preserved_detail[reason] = preserved_detail.get(reason, 0) + 1
            if sub == "task_started":
                turn += 1  # native turn boundary; no inference, and no event emitted
            continue

        if wtype in ("turn_context", "world_state"):
            preserved_detail[wtype] = preserved_detail.get(wtype, 0) + 1
            if wtype == "turn_context" and p.get("model"):
                models.add(p["model"])
            continue

        if wtype == "compacted":
            # Observed that a compaction occurred; its metrics are NOT safely
            # interpretable yet, so source is 'unavailable' and every metric is
            # null. Characterizing the payload is allowed during calibration;
            # representing it richer requires a version bump, not a 0.2 edit.
            emit(
                event_id=_event_id(session_id, line_no, None),
                event_type="compaction",
                event_origin="observed",
                synthesis_reason=None,
                timestamp=ts,
                raw_source_ref=ref(line_no, raw),
                field_status={k: "unavailable" for k in (
                    "trigger", "pre_tokens", "post_tokens",
                    "cumulative_dropped_tokens", "duration_ms")} | {
                    "compaction_source": "observed"},
                compaction={
                    "compaction_source": "unavailable",
                    "trigger": None, "pre_tokens": None, "post_tokens": None,
                    "cumulative_dropped_tokens": None, "duration_ms": None,
                },
            )
            continue

        # wtype == "response_item"
        ptype = p.get("type")

        if ptype == "message":
            role = p.get("role")
            if role not in CANONICAL_ROLES:
                # `developer` (system-injected) is not one of the canonical
                # message classes; retained and counted, not normalized.
                preserve(f"message_role_{role}")
                continue
            if role == "user":
                emit(
                    event_id=_event_id(session_id, line_no, None),
                    event_type="user_message",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=ref(line_no, raw),
                    field_status={"prompt_source": "unavailable"},
                    # One protocol-level record. NOT a prompt, NOT a turn:
                    # several may share one task_started turn.
                    user_message={"prompt_source": None},
                )
            else:
                emit(
                    event_id=_event_id(session_id, line_no, None),
                    event_type="assistant_message",
                    event_origin="observed",
                    synthesis_reason=None,
                    timestamp=ts,
                    raw_source_ref=ref(line_no, raw),
                    field_status={k: "unavailable" for k in (
                        "input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")},
                    # Codex reports tokens only as a separate cumulative stream
                    # with no 1:1 response mapping. No join is attempted.
                    assistant_message={
                        "input_tokens": None, "output_tokens": None,
                        "cache_read_input_tokens": None,
                        "cache_creation_input_tokens": None,
                    },
                )
            continue

        if ptype == "reasoning":
            emit(
                event_id=_event_id(session_id, line_no, None),
                event_type="thinking",
                event_origin="observed",  # the record exists; its size does not
                synthesis_reason=None,
                timestamp=ts,
                raw_source_ref=ref(line_no, raw),
                field_status={"length_value": "unavailable"},
                # encrypted_content is ciphertext — its length is not the
                # plaintext length. Summary text, where present (13 of 2,546
                # corpus records), stays Tier 1 and is never copied here.
                thinking={"length_value": None},
            )
            continue

        if ptype in TOOL_CALL_TYPES:
            cid = p.get("call_id")
            if cid:
                calls.add(cid)
            digest, digest_status = _args_digest(p.get("input"))
            emit(
                event_id=_event_id(session_id, line_no, None),
                event_type="tool_call",
                event_origin="observed",
                synthesis_reason=None,
                timestamp=ts,
                raw_source_ref=ref(line_no, raw),
                field_status={
                    "tool_name": "observed",
                    "tool_correlation_id": "inferred",
                    # Codex tool input is an opaque string with no file_path
                    # key, so no repo-relative target can be derived honestly.
                    "target_path": "unavailable",
                    "args_digest": digest_status,
                    "line_range": "unavailable",
                },
                tool_call={
                    "tool_name": p.get("name"),
                    "tool_correlation_id": (
                        _correlation_id(local_secret, session_id, cid) if cid else None),
                    "target_path": None,
                    "args_digest": digest,
                    "line_range": None,
                },
            )
            continue

        if ptype in TOOL_OUTPUT_TYPES:
            cid = p.get("call_id")
            if cid:
                results.add(cid)
            size, size_status = _output_bytes(p.get("output"))
            emit(
                event_id=_event_id(session_id, line_no, None),
                event_type="tool_result",
                event_origin="observed",
                synthesis_reason=None,
                timestamp=ts,
                raw_source_ref=ref(line_no, raw),
                field_status={
                    "tool_correlation_id": "inferred",
                    # `status: completed` on the CALL is dispatch state, not
                    # result outcome. Codex exposes no error flag on the
                    # output record, so success is genuinely unknown.
                    "success": "unavailable",
                    "result_size_value": size_status,
                    "result_size_method": "observed",
                },
                tool_result={
                    "tool_correlation_id": (
                        _correlation_id(local_secret, session_id, cid) if cid else None),
                    "success": None,
                    "result_size_value": size,
                    "result_size_unit": "utf8_bytes",
                    "result_size_method": "direct" if size is not None else "unavailable",
                },
            )
            continue

        raise UnrecognizedRawType(
            f"{path}:{line_no} has response_item payload type {ptype!r} with no mapping."
        )

    emit(
        event_id=_event_id(session_id, len(raw_lines) + 1, None),
        event_type="session_end",
        event_origin="synthesized",
        synthesis_reason=(
            "Codex emits task_complete per turn but no session-end record; "
            "synthesized from the last observed line at ingestion time."
        ),
        timestamp=parsed[-1][2].get("timestamp") if parsed else None,
        raw_source_ref=None,
        field_status={},
    )

    model_value = next(iter(models)) if len(models) == 1 else None
    consistent = len(models) <= 1
    if not consistent:
        notes.append(f"multiple turn_context models observed: {sorted(models)}")

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
        "pinned_commit": commit,
        "repo_url": git.get("repository_url") or "unknown",
        "disposition": "scope_confounded" if not consistent else disposition,
        "disposition_reason": disposition_reason,
    }

    return CodexAdapterResult(
        events=events,
        run=run,
        snapshot_sha256=snapshot.sha256,
        disposition_counts=disposition_counts,
        preserved_detail=preserved_detail,
        wrapper_counts=wrapper_counts,
        unmatched_calls=sorted(calls - results),
        unmatched_results=sorted(results - calls),
        notes=notes,
    )


# --------------------------------------------------------------------------
# Per-class reconciliation. Ground truth is recomputed from the raw file by
# logic independent of normalize_session — the point is NOT to ask the adapter
# to confirm itself. A coarse total-event ceiling is deliberately not used: it
# would pass while individual classes were wrong in compensating directions.
# --------------------------------------------------------------------------

def raw_class_counts(source: "str | Path | SessionSnapshot") -> dict[str, int]:
    """Count each authoritative raw class straight from the snapshot bytes."""
    c: dict[str, int] = {
        "tool_call": 0, "tool_result": 0, "thinking": 0,
        "user_message": 0, "assistant_message": 0, "compaction": 0, "turns": 0,
        "item_completed": 0, "developer_message": 0,
    }
    for ln in SessionSnapshot.coerce(source).lines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        w, p = r.get("type"), (r.get("payload") or {})
        t = p.get("type")
        if w == "compacted":
            c["compaction"] += 1
        elif w == "event_msg":
            if t == "task_started":
                c["turns"] += 1
            elif t == "item_completed":
                c["item_completed"] += 1
        elif w == "response_item":
            if t == "reasoning":
                c["thinking"] += 1
            elif t in TOOL_CALL_TYPES:
                c["tool_call"] += 1
            elif t in TOOL_OUTPUT_TYPES:
                c["tool_result"] += 1
            elif t == "message":
                role = p.get("role")
                if role == "user":
                    c["user_message"] += 1
                elif role == "assistant":
                    c["assistant_message"] += 1
                else:
                    c["developer_message"] += 1
    return c


def reconcile(
    source: "str | Path | SessionSnapshot", result: CodexAdapterResult
) -> dict[str, dict[str, Any]]:
    """Per-class reconciliation report: {class: {emitted, raw, ok}}.

    Raises SnapshotMismatch if `source` is not byte-identical to what was
    normalized. Passing a path is allowed and safe: it is re-hashed and
    compared, so a file that grew in between fails loudly instead of producing
    a reconciliation across two different file states.
    """
    snapshot = SessionSnapshot.coerce(source)
    snapshot.verify_same_as(result.snapshot_sha256)
    raw = raw_class_counts(snapshot)
    emitted: dict[str, int] = {}
    for ev in result.events:
        emitted[ev["event_type"]] = emitted.get(ev["event_type"], 0) + 1

    report: dict[str, dict[str, Any]] = {}
    for cls in ("tool_call", "tool_result", "thinking", "user_message",
                "assistant_message", "compaction"):
        e, r = emitted.get(cls, 0), raw[cls]
        report[cls] = {"emitted": e, "raw": r, "ok": e == r}

    max_turn = max((ev["turn_index"] for ev in result.events), default=0)
    turns_emitted = max_turn + 1 if raw["turns"] else 0
    report["turns"] = {
        "emitted": turns_emitted, "raw": raw["turns"], "ok": turns_emitted == raw["turns"]}

    # Reported separately, never reconciled into a canonical class.
    report["_preserved"] = {
        "item_completed_raw": raw["item_completed"],
        "item_completed_preserved": result.preserved_detail.get(
            "semantic_projection_item_completed", 0),
        "developer_message_raw": raw["developer_message"],
        "developer_message_preserved": result.preserved_detail.get(
            "message_role_developer", 0),
        "ok": (
            raw["item_completed"] == result.preserved_detail.get(
                "semantic_projection_item_completed", 0)
            and raw["developer_message"] == result.preserved_detail.get(
                "message_role_developer", 0)
        ),
    }
    return report
