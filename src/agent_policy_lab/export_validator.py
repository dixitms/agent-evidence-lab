"""Fail-closed public export validator.

The last thing that runs before anything leaves Tier 1. It refuses on any
failure, reports every reason it found rather than the first, and has **no
partial-export and no best-effort mode** — a validator with an override is a
validator someone will override at 1am before a deadline.

Five checks, in order:

1. **`run_kind`** — only `controlled_public_benchmark` may be exported.
   `private_dogfood` and `calibration` cannot, by any path.
2. **Prohibited fields** — structural. `raw_source_ref`, `args_digest`, and any
   message/thinking/tool-output text key simply must not exist in the payload.
3. **Secret and path scan** — regex over the *serialized* bundle, not its
   declared fields, so a leak smuggled into a string the schema never
   anticipated is still caught.
4. **Human screening** — an explicit checklist, all items confirmed. Automated
   checks catch known shapes; this exists for what they don't.
5. **Fail closed** — any failure in 1-4 refuses.

`export_run()` is the only way to produce a bundle, and it screens *then*
validates, so an unscreened payload never reaches a caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .run_validation import validate_run
from .versions import version_failure

EXPORTABLE_RUN_KIND = "controlled_public_benchmark"

# Structural: these key names must not appear anywhere in an export, at any
# depth. Not "should be empty" — must not exist.
PROHIBITED_KEYS = frozenset({
    "raw_source_ref",      # local paths + content hashes of private material
    "args_digest",         # dropped unless the underlying args are already public
    "local_path",
    "content", "text", "message", "prompt",
    "input", "output", "command",
    "thinking_text", "encrypted_content", "summary",
    "replacement_history",
    # Compaction-continuity annotation fields. That artifact records a human's
    # reading of protected transcript context, so it is Tier 1 terminal and has
    # no screened form; these names make an embedded annotation a structural
    # refusal rather than something screening would have to redact.
    "window_id", "rationale_codes", "review_mode", "reviewer_role",
})

# Fields kept in a Tier 2 derivative.
KEPT_EVENT_FIELDS = (
    "schema_version", "event_id", "sequence_index", "run_id",
    "source_client", "source_client_version", "session_id", "repo_ref",
    "turn_index", "event_type", "event_origin", "timestamp", "field_status",
)
KEPT_PAYLOADS = {
    "tool_call": ("tool_name", "line_range"),
    "tool_result": ("result_size_value", "result_size_unit", "result_size_method"),
    "thinking": ("length_value",),
    "compaction": ("compaction_source", "trigger", "pre_tokens", "post_tokens",
                   "cumulative_dropped_tokens", "duration_ms"),
    "assistant_message": ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"),
    "user_message": ("prompt_source",),
}

REQUIRED_MANIFEST_FIELDS = (
    "manifest_version", "run_id", "source_client", "schema_version",
    "event_count", "session_snapshot_sha256", "pinned_commit", "repo_url",
    "run_kind", "condition", "disposition", "reconciled_ok",
)

REQUIRED_CHECKLIST = (
    "reviewed_every_exported_field",
    "confirmed_no_prompt_or_tool_output_text",
    "confirmed_public_repo_and_pinned_commit",
    "confirmed_sample_size_and_scope_stated",
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("posix home path", re.compile(r"/(?:Users|home)/[^/\s\"]+")),
    ("windows home path", re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\\\s\"]+")),
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{8,}")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class ExportRefused(Exception):
    """Export halted. Carries every reason found, not just the first."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(
            "export refused ("
            + str(len(reasons))
            + " reason"
            + ("s" if len(reasons) != 1 else "")
            + "):\n  - "
            + "\n  - ".join(reasons)
        )


@dataclass(frozen=True)
class HumanSignoff:
    """The manual step. Automated checks catch known shapes; this catches the rest."""

    operator: str
    date: str
    checklist: dict[str, bool] = field(default_factory=dict)

    def failures(self) -> list[str]:
        out: list[str] = []
        if not self.operator.strip():
            out.append("human screening: no operator recorded")
        if not self.date.strip():
            out.append("human screening: no date recorded")
        for item in REQUIRED_CHECKLIST:
            if self.checklist.get(item) is not True:
                out.append(f"human screening: checklist item not confirmed: {item}")
        unknown = set(self.checklist) - set(REQUIRED_CHECKLIST)
        if unknown:
            out.append(
                f"human screening: unrecognized checklist items {sorted(unknown)} — "
                f"the checklist is fixed, not extended at export time"
            )
        return out


def _walk_keys(node: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}/{k}"
            yield k, here
            yield from _walk_keys(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_keys(v, f"{path}/{i}")


def screen_run(
    events: list[dict[str, Any]],
    run: dict[str, Any],
    manifest: dict[str, Any],
    *,
    session_salt: bytes,
) -> dict[str, Any]:
    """Produce the Tier 2 derivative. Screening alone does not authorise export."""
    def hashed_session(sid: str) -> str:
        return "sha256:" + hashlib.sha256(session_salt + sid.encode("utf-8")).hexdigest()

    public_ref = f"{manifest.get('repo_url')}@{manifest.get('pinned_commit')}"
    screened: list[dict[str, Any]] = []
    for ev in events:
        out = {k: ev[k] for k in KEPT_EVENT_FIELDS if k in ev}
        out["session_id"] = hashed_session(str(ev.get("session_id", "")))
        out["repo_ref"] = public_ref

        kept_field_names: set[str] = set()
        for payload, keep in KEPT_PAYLOADS.items():
            if payload in ev:
                out[payload] = {k: ev[payload][k] for k in keep if k in ev[payload]}
                kept_field_names |= set(out[payload])

        # field_status is filtered to the fields that actually survived. Keeping
        # it verbatim advertised a status for every dropped field — a Tier 2
        # artifact announcing 'args_digest: observed' while carrying no digest
        # is both incoherent and a needless hint about what Tier 1 holds.
        out["field_status"] = {
            k: v for k, v in (ev.get("field_status") or {}).items()
            if k in kept_field_names
        }
        screened.append(out)

    return {
        "bundle_version": "1.0.0",
        "manifest": {
            "manifest_version": manifest.get("manifest_version"),
            "schema_version": manifest.get("schema_version"),
            "source_client": manifest.get("source_client"),
            "source_client_version": manifest.get("source_client_version"),
            "repo_url": manifest.get("repo_url"),
            "pinned_commit": manifest.get("pinned_commit"),
            "capture_date": manifest.get("capture_date"),
            "event_count": manifest.get("event_count"),
            "session_snapshot_sha256": manifest.get("session_snapshot_sha256"),
            "task_id": manifest.get("task_id"),
            "policy_id": manifest.get("policy_id"),
            "policy_version": manifest.get("policy_version"),
            "policy_text_sha256": manifest.get("policy_text_sha256"),
            "task_prompt_sha256": manifest.get("task_prompt_sha256"),
            "run_kind": manifest.get("run_kind"),
            "condition": manifest.get("condition"),
            "disposition": manifest.get("disposition"),
            "reconciled_ok": manifest.get("reconciled_ok"),
            # Hashes and verdicts only — never the prompt or policy text itself.
            "delivered_prompt_sha256": manifest.get("delivered_prompt_sha256"),
            "prompt_delivery_verified": manifest.get("delivered_prompt_sha256") is not None,
        },
        "events": screened,
    }


def validate_export(
    bundle: dict[str, Any],
    *,
    run: dict[str, Any],
    manifest: dict[str, Any],
    signoff: HumanSignoff | None,
) -> None:
    """Raise ExportRefused unless every check passes. Never returns a partial pass."""
    reasons: list[str] = []

    # 0. format contracts. A bundle or manifest stamped with a version this
    #    build does not understand is refused rather than read with the wrong
    #    assumptions.
    checks = [("bundle_version", bundle.get("bundle_version")),
              ("manifest_version", manifest.get("manifest_version"))]
    if "ledger" in bundle:
        # A bundle carrying attempt history must declare a ledger version this
        # build understands, on the same fail-closed terms as every other
        # contract — otherwise replacements could be shown under unknown rules.
        checks.append(("ledger_version", (bundle["ledger"] or {}).get("ledger_version")))
    for kind, value in checks:
        failure = version_failure(kind, value)
        if failure:
            reasons.append(f"format check: {failure}")

    # 1. run_kind — no override exists, deliberately
    for label, source in (("run record", run), ("manifest", manifest)):
        kind = source.get("run_kind")
        if kind != EXPORTABLE_RUN_KIND:
            reasons.append(
                f"run_kind check: {label} has run_kind={kind!r}; only "
                f"{EXPORTABLE_RUN_KIND!r} may be exported"
            )
    if run.get("run_kind") != manifest.get("run_kind"):
        reasons.append("run_kind check: run record and manifest disagree on run_kind")

    # manifest integrity — a malformed manifest is not provenance
    for f in REQUIRED_MANIFEST_FIELDS:
        if manifest.get(f) in (None, ""):
            reasons.append(f"manifest check: required field missing or empty: {f}")
    if manifest.get("reconciled_ok") is not True:
        reasons.append(
            "manifest check: reconciled_ok is not true — unreconciled data cannot "
            "become public evidence"
        )
    # A placeholder is not an anchor. Dogfood ingestion legitimately records
    # pinned_commit='unknown' and repo_url='local-private'; neither may satisfy
    # the public-provenance requirement just by being non-empty.
    commit = str(manifest.get("pinned_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        reasons.append(
            f"manifest check: pinned_commit={commit!r} is not a git commit hash — "
            f"public evidence must name the exact commit it was produced at"
        )
    repo_url = str(manifest.get("repo_url") or "")
    if not repo_url.startswith(("https://", "http://")):
        reasons.append(
            f"manifest check: repo_url={repo_url!r} is not a public URL — a local "
            f"or placeholder repository cannot back a public artifact"
        )

    declared = manifest.get("event_count")
    actual = len(bundle.get("events", []))
    if declared != actual:
        reasons.append(
            f"manifest check: event_count={declared} but bundle carries {actual} events"
        )

    # 2. prohibited fields — structural, at any depth
    for key, path in _walk_keys(bundle):
        if key in PROHIBITED_KEYS:
            reasons.append(f"prohibited field: {key!r} present at {path}")

    # 3. secret/path scan over the serialized payload
    serialized = json.dumps(bundle, sort_keys=True)
    for label, pattern in SECRET_PATTERNS:
        for m in pattern.finditer(serialized):
            snippet = m.group(0)
            shown = snippet[:12] + "…" if len(snippet) > 12 else snippet
            reasons.append(f"secret/path scan: {label} matched ({shown!r})")

    # 4. human screening
    if signoff is None:
        reasons.append("human screening: no sign-off supplied")
    else:
        reasons.extend(signoff.failures())

    # 5. fail closed
    if reasons:
        raise ExportRefused(reasons)


def export_run(
    events: list[dict[str, Any]],
    run: dict[str, Any],
    manifest: dict[str, Any],
    *,
    session_salt: bytes,
    signoff: HumanSignoff | None,
) -> dict[str, Any]:
    """Validate the Tier 1 source, then screen, then validate the bundle.

    The source check is not redundant with the bundle check. Screening is a
    lossy transform: it drops fields and rewrites identity, so an invalid,
    mixed-version, unreconciled or manifest-disagreeing *source* can be turned
    into a clean-looking Tier 2 bundle if a caller supplies a plausible
    manifest. Validating the input first means an export can only be built from
    data that was itself validated.
    """
    source = validate_run(events, run, manifest)
    if not source.ok:
        raise ExportRefused(
            ["Tier 1 source is not validated data; refusing to screen it"]
            + [f"source schema: {r}" for r in source.schema_errors]
            + [f"source dataset: {r}" for r in source.dataset_findings]
            + [f"source reconciliation: {r}" for r in source.reconciliation_failures]
            + [f"source manifest: {r}" for r in source.manifest_disagreements]
        )
    bundle = screen_run(events, run, manifest, session_salt=session_salt)
    validate_export(bundle, run=run, manifest=manifest, signoff=signoff)
    return bundle
