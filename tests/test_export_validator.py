"""Fail-closed export validator — red-team first, happy path second.

The happy path is one test. The other twenty-one deliberately attempt to smuggle
something past the boundary and must be refused. That ratio is the point: this
is the control standing between Tier 1 and a published artifact, and a control
whose refusals nobody has watched is indistinguishable from one that always
returns ok.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_policy_lab.export_validator import (
    ExportRefused,
    HumanSignoff,
    REQUIRED_CHECKLIST,
    export_run,
    screen_run,
    validate_export,
)

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "fixtures/export/redteam_cases.json").read_text())
SALT = b"per-export-salt"
POSIX_HOME_PREFIX = "/" + "Users/"

_REDTEAM_VALUES = {
    "__redteam_value__:posix_home_path": POSIX_HOME_PREFIX + "fixture-user/Git/private-repo/task1",
    "__redteam_value__:windows_home_path": "C:" + "\\" + "Users" + "\\" + "fixture-user\\private\\task",
    "__redteam_value__:email_address": "reported-by-" + "fixture" + "@" + "example.invalid",
    "__redteam_value__:anthropic_key": "sk" + "-ant-" + "api03-AAAABBBBCCCCDDDD",
    "__redteam_value__:github_token": "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "__redteam_value__:aws_key": "A" + "KIA" + "IOSFODNN7EXAMPLE",
    "__redteam_value__:bearer_token": "Authorization " + "Bear" + "er " + "abcdef1234567890",
    "__redteam_value__:private_key_block": "-" * 5 + "BEGIN RSA " + "PRIVATE" + " KEY" + "-" * 5,
}


def good_signoff() -> HumanSignoff:
    return HumanSignoff(
        operator="operator",
        date="2026-08-19",
        checklist={item: True for item in REQUIRED_CHECKLIST},
    )


def exportable_run():
    """A controlled public-benchmark run — the only kind that may be exported."""
    run = {
        "schema_version": "0.1.1",
        "run_id": "6f1d3a2e-0c4b-4f8a-9e21-5b7c8d9e0a11",
        "run_kind": "controlled_public_benchmark",
        "condition": "baseline",
        "policy_id": None,
        "policy_version": None,
        "model": "claude-sonnet-5",
        "model_source": "declared_preflight",
        "model_evidence_consistent": True,
        "task_id": "apl-d-t1",
        "pinned_commit": "874ca2bc1c30d93a4ac6e36a15ed685eafe89097",
        "repo_url": "https://github.com/pallets/click",
        "disposition": "completed_analyzable",
    }
    manifest = {
        "manifest_version": "1.0.0",
        "run_id": run["run_id"],
        "source_client": "claude-code",
        "source_client_version": "2.1.220",
        "schema_version": "0.1.1",
        "event_count": 2,
        "session_snapshot_sha256": "a" * 64,
        "raw_local_path": "/TIER1/raw/session.jsonl",
        "task_id": "apl-d-t1",
        "pinned_commit": run["pinned_commit"],
        "repo_url": run["repo_url"],
        "policy_id": None,
        "policy_version": None,
        "policy_text_sha256": "e" * 64,
        "task_prompt_sha256": "f" * 64,
        "model": "claude-sonnet-5",
        "model_source": "declared_preflight",
        "model_evidence_consistent": True,
        "run_kind": "controlled_public_benchmark",
        "condition": "baseline",
        "disposition": "completed_analyzable",
        "disposition_reason": None,
        "capture_date": "2026-08-19T00:00:00+00:00",
        "reconciled_ok": True,
        "reconciliation": {"tool_call": {"emitted": 1, "raw": 1, "ok": True}},
    }
    events = [
        {
            "schema_version": "0.1.1", "event_id": "11111111-2222-4333-8444-555555555555",
            "sequence_index": 0, "run_id": run["run_id"], "source_client": "claude-code",
            "source_client_version": "2.1.220",
            "session_id": "a-real-local-session-id", "repo_ref": "sha256:localhash",
            "turn_index": 0, "event_type": "tool_call", "event_origin": "observed",
            "timestamp": "2026-08-19T10:00:00.000Z",
            "field_status": {"tool_name": "observed", "tool_correlation_id": "inferred",
                             "target_path": "observed", "args_digest": "observed",
                             "line_range": "observed"},
            "raw_source_ref": {"local_path": POSIX_HOME_PREFIX + "fixture-user/.claude/x.jsonl",
                               "line_or_offset": 3, "json_pointer": None,
                               "content_sha256": "c" * 64},
            "tool_call": {"tool_name": "Read", "tool_correlation_id": "hmac:" + "d" * 64,
                          "target_path": "src/click/core.py",
                          "args_digest": "sha256:" + "b" * 64,
                          "line_range": {"start": 1, "end": 40}},
        },
        {
            "schema_version": "0.1.1", "event_id": "22222222-2222-4333-8444-555555555555",
            "sequence_index": 1, "run_id": run["run_id"], "source_client": "claude-code",
            "source_client_version": "2.1.220",
            "session_id": "a-real-local-session-id", "repo_ref": "sha256:localhash",
            "turn_index": 0, "event_type": "tool_result", "event_origin": "observed",
            "timestamp": "2026-08-19T10:00:01.000Z",
            "field_status": {"tool_correlation_id": "inferred", "success": "observed",
                             "result_size_value": "observed",
                             "result_size_method": "observed"},
            "raw_source_ref": {"local_path": POSIX_HOME_PREFIX + "fixture-user/.claude/x.jsonl",
                               "line_or_offset": 4, "json_pointer": None,
                               "content_sha256": "c" * 64},
            "tool_result": {"tool_correlation_id": "hmac:" + "d" * 64, "success": True,
                            "result_size_value": 4821, "result_size_unit": "utf8_bytes",
                            "result_size_method": "direct"},
        },
    ]
    return events, run, manifest


# --- the one happy path --------------------------------------------------


def test_screened_controlled_run_exports():
    events, run, manifest = exportable_run()
    bundle = export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())
    assert len(bundle["events"]) == 2
    assert bundle["manifest"]["pinned_commit"] == run["pinned_commit"]


def test_screening_removes_local_identity_and_private_fields():
    events, run, manifest = exportable_run()
    bundle = export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())
    blob = json.dumps(bundle)

    assert "a-real-local-session-id" not in blob, "raw session id must be hashed"
    assert bundle["events"][0]["session_id"].startswith("sha256:")
    assert POSIX_HOME_PREFIX not in blob and "raw_source_ref" not in blob
    assert "args_digest" not in blob
    assert "tool_correlation_id" not in blob, "correlation ids are Tier 1 identity"
    # what survives is the measurement
    assert bundle["events"][0]["tool_call"] == {
        "tool_name": "Read", "line_range": {"start": 1, "end": 40}}
    assert bundle["events"][1]["tool_result"]["result_size_value"] == 4821
    assert bundle["events"][0]["repo_ref"] == (
        "https://github.com/pallets/click@874ca2bc1c30d93a4ac6e36a15ed685eafe89097")


def test_salt_changes_the_session_hash():
    events, run, manifest = exportable_run()
    a = export_run(events, run, manifest, session_salt=b"salt-a", signoff=good_signoff())
    b = export_run(events, run, manifest, session_salt=b"salt-b", signoff=good_signoff())
    assert a["events"][0]["session_id"] != b["events"][0]["session_id"]


# --- red team: every case must be refused, for its own reason ------------


def _materialize_redteam_value(value):
    if isinstance(value, str):
        return _REDTEAM_VALUES.get(value, value)
    if isinstance(value, list):
        return [_materialize_redteam_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _materialize_redteam_value(item) for key, item in value.items()}
    return value


def _apply(case):
    events, run, manifest = exportable_run()
    bundle = screen_run(events, run, manifest, session_salt=SALT)
    signoff = good_signoff()
    target, mut = case["target"], _materialize_redteam_value(case["mutation"])

    if target == "run":
        run.update(mut)
        manifest.update(mut)          # keep them agreeing; the check under test is run_kind
    elif target == "manifest":
        manifest.update(mut)
        bundle["manifest"].update(mut)
    elif target == "manifest_extra":
        manifest.update(mut)
        bundle["manifest"].update(mut)
    elif target == "manifest_del":
        for k in mut:
            manifest.pop(k, None)
    elif target == "event":
        bundle["events"][0].update(copy.deepcopy(mut))
    elif target == "event_payload":
        for payload, body in mut.items():
            bundle["events"][0].setdefault(payload, {}).update(body)
    elif target == "signoff":
        signoff = None
    elif target == "signoff_partial":
        signoff = HumanSignoff(operator="operator", date="2026-08-19",
                               checklist={k: True for k in mut})
    elif target == "signoff_extra":
        signoff = HumanSignoff(operator="operator", date="2026-08-19",
                               checklist={**{k: True for k in REQUIRED_CHECKLIST}, **mut})
    else:  # pragma: no cover - a case file typo should fail loudly
        raise AssertionError(f"unknown target {target!r}")
    return bundle, run, manifest, signoff


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_redteam_case_is_refused(case):
    bundle, run, manifest, signoff = _apply(case)
    with pytest.raises(ExportRefused) as excinfo:
        validate_export(bundle, run=run, manifest=manifest, signoff=signoff)
    joined = "\n".join(excinfo.value.reasons)
    assert case["expect_reason_contains"] in joined, (
        f"{case['name']} was refused, but not for its own reason.\n"
        f"expected fragment: {case['expect_reason_contains']}\n"
        f"actual reasons:\n{joined}"
    )


def test_every_redteam_case_would_pass_if_unmutated():
    """Proves each refusal is caused by its mutation, not by a broken baseline."""
    events, run, manifest = exportable_run()
    bundle = screen_run(events, run, manifest, session_salt=SALT)
    validate_export(bundle, run=run, manifest=manifest, signoff=good_signoff())


def test_refusal_reports_every_reason_not_just_the_first():
    events, run, manifest = exportable_run()
    bundle = screen_run(events, run, manifest, session_salt=SALT)
    run["run_kind"] = manifest["run_kind"] = "private_dogfood"
    manifest["reconciled_ok"] = False
    bundle["events"][0]["raw_source_ref"] = {"local_path": POSIX_HOME_PREFIX + "fixture-user/x"}
    with pytest.raises(ExportRefused) as excinfo:
        validate_export(bundle, run=run, manifest=manifest, signoff=None)
    reasons = excinfo.value.reasons
    assert len(reasons) >= 5
    kinds = {r.split(":")[0] for r in reasons}
    assert {"run_kind check", "manifest check", "prohibited field",
            "secret/path scan", "human screening"} <= kinds


def test_there_is_no_override_or_partial_export():
    """export_run has no force/allow flag, and screening alone does not authorise."""
    import inspect
    from agent_policy_lab import export_validator as mod

    params = set(inspect.signature(mod.export_run).parameters)
    assert not params & {"force", "allow", "override", "skip_checks", "best_effort"}
    events, run, manifest = exportable_run()
    run["run_kind"] = manifest["run_kind"] = "private_dogfood"
    with pytest.raises(ExportRefused):
        export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())


# --- item 1: the Tier 1 SOURCE is validated before screening -------------
# Screening is lossy: it drops fields and rewrites identity, so an invalid
# source plus a plausible manifest could otherwise become a clean-looking
# bundle. These four prove the source itself must be validated data.


def test_export_refuses_source_with_schema_violation():
    events, run, manifest = exportable_run()
    events[0]["event_type"] = "not_a_real_event_type"
    with pytest.raises(ExportRefused, match="source schema"):
        export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())


def test_export_refuses_source_with_dataset_finding():
    events, run, manifest = exportable_run()
    events[1]["sequence_index"] = events[0]["sequence_index"]
    with pytest.raises(ExportRefused, match="source dataset"):
        export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())


def test_export_refuses_source_with_failed_reconciliation():
    events, run, manifest = exportable_run()
    manifest["reconciliation"]["tool_call"]["ok"] = False
    with pytest.raises(ExportRefused, match="source reconciliation"):
        export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())


def test_export_refuses_source_with_manifest_disagreement():
    events, run, manifest = exportable_run()
    manifest["event_count"] = 99
    with pytest.raises(ExportRefused, match="source manifest"):
        export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())


def test_export_refuses_mixed_version_source():
    """The case that motivated the check: a mixed-version source could have been
    screened into a bundle that looks single-version."""
    events, run, manifest = exportable_run()
    events[1]["schema_version"] = "0.2.0"
    with pytest.raises(ExportRefused, match="never pooled"):
        export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())


# --- item 2: format versions are contracts -------------------------------


@pytest.mark.parametrize("bad", ["2.0.0", "1.0", "", None, "latest", 1.0])
def test_unsupported_bundle_version_fails_closed(bad):
    events, run, manifest = exportable_run()
    bundle = screen_run(events, run, manifest, session_salt=SALT)
    bundle["bundle_version"] = bad
    with pytest.raises(ExportRefused, match="bundle_version"):
        validate_export(bundle, run=run, manifest=manifest, signoff=good_signoff())


@pytest.mark.parametrize("bad", ["2.0.0", "1.0", "", None, "nightly"])
def test_unsupported_manifest_version_fails_closed(bad):
    events, run, manifest = exportable_run()
    manifest["manifest_version"] = bad
    bundle = screen_run(events, run, manifest, session_salt=SALT)
    with pytest.raises(ExportRefused, match="manifest_version"):
        validate_export(bundle, run=run, manifest=manifest, signoff=good_signoff())


# --- ledger in an export: versioned and fail-closed like everything else --


def _bundle_with_ledger(version="2.0.0"):
    events, run, manifest = exportable_run()
    bundle = screen_run(events, run, manifest, session_salt=SALT)
    bundle["ledger"] = {
        "ledger_version": version,
        "runs": [{"run_ref": "sha256:x", "pair_id": "apl-d-t1",
                  "task_id": "apl-d-t1", "condition": "baseline",
                  "disposition": "completed_analyzable"}],
        "replacements": [],
    }
    return bundle, run, manifest


def test_bundle_with_a_supported_ledger_version_is_accepted():
    bundle, run, manifest = _bundle_with_ledger()
    validate_export(bundle, run=run, manifest=manifest, signoff=good_signoff())


@pytest.mark.parametrize("bad", ["3.0.0", "1.0", "", None, "latest"])
def test_bundle_with_an_unsupported_ledger_version_is_refused(bad):
    """Replacements must not be shown under rules this build cannot read."""
    bundle, run, manifest = _bundle_with_ledger(version=bad)
    with pytest.raises(ExportRefused, match="ledger_version"):
        validate_export(bundle, run=run, manifest=manifest, signoff=good_signoff())


def test_prompt_verification_verdict_travels_but_the_prompt_does_not():
    events, run, manifest = exportable_run()
    manifest["delivered_prompt_sha256"] = "d" * 64
    bundle = export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())
    assert bundle["manifest"]["prompt_delivery_verified"] is True
    assert bundle["manifest"]["delivered_prompt_sha256"] == "d" * 64
    assert "normalize(value)" not in json.dumps(bundle), "hashes and verdicts only"


def test_unverified_prompt_is_visible_as_a_verdict():
    events, run, manifest = exportable_run()
    bundle = export_run(events, run, manifest, session_salt=SALT, signoff=good_signoff())
    assert bundle["manifest"]["prompt_delivery_verified"] is False
