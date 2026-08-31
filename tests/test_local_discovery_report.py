"""Local discovery report: generic contract validation and exclusive-create writer.

This file uses synthetic fixtures only. The generic hygiene guard below rejects
the shape of a direct trace identifier or home path within this public contract
and its fixtures; it never hard-codes a private value.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
from pathlib import Path

import jsonschema
import pytest

from agent_evidence_lab.local_discovery_report import (
    DEFAULT_ROOT,
    LocalDiscoveryAvailabilityMismatch,
    LocalDiscoveryCoverageMismatch,
    LocalDiscoveryDuplicateName,
    LocalDiscoveryDuplicateReport,
    LocalDiscoveryPathError,
    LocalDiscoveryProvenanceBasisInvalid,
    LocalDiscoveryProvenanceInvalid,
    LocalDiscoveryReconciliationShapeInvalid,
    LocalDiscoveryScopeMismatch,
    LocalDiscoveryScopeNotSupported,
    LocalDiscoverySignalNotDeclared,
    LocalDiscoveryUnsafeReportId,
    LocalDiscoveryWriteError,
    UnsupportedReportVersion,
    create_report,
    validate_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures" / "local_discovery"


def _load(*parts: str) -> dict:
    return json.loads((FIXTURES.joinpath(*parts)).read_text())


def valid_report() -> dict:
    return _load("valid", "report.json")


# ---------------------------------------------------------------------------
# Fixture/planning/review hygiene: a generic shape guard, not a value deny-list.
# ---------------------------------------------------------------------------

# A real session id looks like a UUID (Claude Code's convention); this contract's
# fixtures use short placeholders ("S1".."S5") instead, so a UUID-shaped string
# anywhere in these files would mean a real id leaked back in.
_UUID_SHAPE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# An absolute home path, or Claude Code's dash-encoded project-directory form of one,
# with an actual username segment -- not a bare "~/.claude/projects" mention, which
# names a known tool convention with nobody's identity in it. Extended beyond
# macOS to Linux and Windows forms, ahead of a future GCP/Linux workstation pass --
# still shape-only, no value-specific deny-list.
_HOME_PATH_SHAPE = re.compile(
    r"(?:/" + r"Users/[A-Za-z0-9_.]+(?:/|\b))"  # macOS
    r"|(?:-Users-[A-Za-z0-9_.]+-)"                 # Claude Code's macOS dash-encoding
    r"|(?:/" + r"home/[A-Za-z0-9_.]+(?:/|\b))"   # Linux
    r"|(?:-home-[A-Za-z0-9_.]+-)"                  # Claude Code's Linux dash-encoding
    r"|(?:[A-Za-z]:\\" + r"Users\\[A-Za-z0-9_.]+(?:\\|\b))"  # Windows
)

_RAW_LOCAL_DISCOVERY_FIXTURES = REPO_ROOT / "fixtures" / "raw" / "local_discovery"

_SCANNED_FILES = sorted(FIXTURES.rglob("*.json")) + [
    REPO_ROOT / "docs" / "local-discovery-report-contract.md",
    # A not-yet-existing directory's rglob() is empty, so this guard applies to
    # a raw local-discovery fixture as soon as one is added.
    *sorted(p for p in _RAW_LOCAL_DISCOVERY_FIXTURES.rglob("*") if p.is_file()),
]


def test_local_discovery_files_contain_no_uuid_or_home_path_shapes():
    """Reject identifier/path shapes within the public report contract and fixtures."""
    assert len(_SCANNED_FILES) >= 5, "the scanned file list looks wrong -- check the globs"
    for path in _SCANNED_FILES:
        text = path.read_text()
        uuid_hits = _UUID_SHAPE.findall(text)
        assert not uuid_hits, f"{path} contains a UUID-shaped string: {uuid_hits}"
        home_hits = _HOME_PATH_SHAPE.findall(text)
        assert not home_hits, f"{path} contains a home-path-shaped string: {home_hits}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_report_passes():
    validate_report(valid_report())  # must not raise


# ---------------------------------------------------------------------------
# Schema-shape validation (jsonschema itself, not a project-specific exception)
# ---------------------------------------------------------------------------


def test_schema_missing_required_field_refused():
    with pytest.raises(jsonschema.exceptions.ValidationError):
        validate_report(_load("invalid", "schema_missing_scope.json"))


def test_missing_snapshot_hash_refused():
    with pytest.raises(jsonschema.exceptions.ValidationError):
        validate_report(_load("invalid", "missing_snapshot_hash.json"))


def test_snapshot_sha256_wrong_shape_refused():
    with pytest.raises(jsonschema.exceptions.ValidationError):
        validate_report(_load("invalid", "snapshot_sha256_wrong_shape.json"))


# ---------------------------------------------------------------------------
# Version registration (this module's own fail-closed mechanism -- see
# UnsupportedReportVersion's docstring for why it does not extend versions.py)
# ---------------------------------------------------------------------------


def test_unsupported_report_version_refused():
    with pytest.raises(UnsupportedReportVersion):
        validate_report(_load("invalid", "unsupported_version.json"))


# ---------------------------------------------------------------------------
# report_id safety
# ---------------------------------------------------------------------------


def test_unsafe_report_id_traversal_refused():
    with pytest.raises(LocalDiscoveryUnsafeReportId):
        validate_report(_load("invalid", "unsafe_report_id_traversal.json"))


def test_unsafe_report_id_slash_refused():
    with pytest.raises(LocalDiscoveryUnsafeReportId):
        validate_report(_load("invalid", "unsafe_report_id_slash.json"))


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


def test_duplicate_session_id_refused():
    with pytest.raises(LocalDiscoveryDuplicateName):
        validate_report(_load("invalid", "duplicate_session_id.json"))


def test_duplicate_diagnostic_name_refused():
    with pytest.raises(LocalDiscoveryDuplicateName):
        validate_report(_load("invalid", "duplicate_diagnostic_name.json"))


# ---------------------------------------------------------------------------
# Structured scope validation
# ---------------------------------------------------------------------------


def test_scope_session_count_mismatch_refused():
    with pytest.raises(LocalDiscoveryScopeMismatch):
        validate_report(_load("invalid", "scope_mismatch.json"))


# ---------------------------------------------------------------------------
# Coverage: availability keys, results keys, and per_session keys must exactly
# equal diagnostic names / input session ids (availability + per_session too)
# ---------------------------------------------------------------------------


def test_availability_coverage_missing_refused():
    with pytest.raises(LocalDiscoveryCoverageMismatch):
        validate_report(_load("invalid", "availability_coverage_missing.json"))


def test_availability_coverage_extra_refused():
    with pytest.raises(LocalDiscoveryCoverageMismatch):
        validate_report(_load("invalid", "availability_coverage_extra.json"))


def test_coverage_missing_diagnostic_refused():
    with pytest.raises(LocalDiscoveryCoverageMismatch):
        validate_report(_load("invalid", "coverage_missing_diagnostic.json"))


def test_coverage_extra_diagnostic_refused():
    with pytest.raises(LocalDiscoveryCoverageMismatch):
        validate_report(_load("invalid", "coverage_extra_diagnostic.json"))


def test_per_session_coverage_missing_refused():
    with pytest.raises(LocalDiscoveryCoverageMismatch):
        validate_report(_load("invalid", "per_session_coverage_missing.json"))


def test_per_session_coverage_extra_refused():
    with pytest.raises(LocalDiscoveryCoverageMismatch):
        validate_report(_load("invalid", "per_session_coverage_extra.json"))


# ---------------------------------------------------------------------------
# Availability-count reconciliation: internal sum, vs. scope, and
# vs. what the per_session entries actually say
# ---------------------------------------------------------------------------


def test_availability_tuple_internal_mismatch_refused():
    with pytest.raises(LocalDiscoveryAvailabilityMismatch):
        validate_report(_load("invalid", "availability_tuple_internal_mismatch.json"))


def test_availability_session_count_mismatch_refused():
    with pytest.raises(LocalDiscoveryAvailabilityMismatch):
        validate_report(_load("invalid", "availability_session_count_mismatch.json"))


def test_session_outcome_reconciliation_mismatch_refused():
    """The tuple is internally consistent and matches scope.session_count, but does
    not match the actual outcomes recorded in per_session."""
    with pytest.raises(LocalDiscoveryAvailabilityMismatch):
        validate_report(_load("invalid", "session_outcome_reconciliation_mismatch.json"))


# ---------------------------------------------------------------------------
# Provenance validation -- per_session leaves (full: inputs + field_status +
# rule_version) and aggregate leaves (validated too, not skipped --
# lighter requirement, just rule_version, since an aggregate summarizes
# already-validated per-session leaves rather than reading canonical fields)
# ---------------------------------------------------------------------------


def test_provenance_missing_rule_version_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "provenance_missing_rule_version.json"))


def test_provenance_missing_field_status_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_missing_field_status.json"))


# ---------------------------------------------------------------------------
# Pre-use contract correction: reconciliation_availability's own
# explicit {raw_count, canonical_count, delta} / {raw_count, preserved_count,
# delta} shape, and its honestly-typed adapter_reconciliation provenance --
# not a canonical field with field_status, and not the adapter's own
# {emitted, raw, ok} shape passed through verbatim.
# ---------------------------------------------------------------------------


def test_reconciliation_shape_missing_ordinary_key_refused():
    with pytest.raises(LocalDiscoveryReconciliationShapeInvalid):
        validate_report(_load("invalid", "reconciliation_shape_missing_ordinary_key.json"))


def test_reconciliation_shape_carries_ok_refused():
    with pytest.raises(LocalDiscoveryReconciliationShapeInvalid):
        validate_report(_load("invalid", "reconciliation_shape_carries_ok.json"))


def test_reconciliation_shape_bad_ordinary_triplet_refused():
    with pytest.raises(LocalDiscoveryReconciliationShapeInvalid):
        validate_report(_load("invalid", "reconciliation_shape_bad_ordinary_triplet.json"))


def test_reconciliation_shape_preserved_missing_category_refused():
    with pytest.raises(LocalDiscoveryReconciliationShapeInvalid):
        validate_report(_load("invalid", "reconciliation_shape_preserved_missing_category.json"))


def test_reconciliation_shape_preserved_bad_triplet_refused():
    with pytest.raises(LocalDiscoveryReconciliationShapeInvalid):
        validate_report(_load("invalid", "reconciliation_shape_preserved_bad_triplet.json"))


def test_reconciliation_shape_missing_key_aggregate_refused():
    """The aggregate leaf is checked identically to per-session leaves -- not a
    lesser or skipped check."""
    with pytest.raises(LocalDiscoveryReconciliationShapeInvalid):
        validate_report(_load("invalid", "reconciliation_shape_missing_key_aggregate.json"))


def test_reconciliation_provenance_missing_source_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "reconciliation_provenance_missing_source.json"))


def test_reconciliation_provenance_wrong_source_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "reconciliation_provenance_wrong_source.json"))


def test_reconciliation_availability_is_adapter_reconciliation_measure_kind():
    """Not 'derived' -- it isn't a canonical field with a field_status, it's
    produced by the adapter's own reconciliation step."""
    report = valid_report()
    diagnostic = next(d for d in report["diagnostics"] if d["name"] == "reconciliation_availability")
    assert diagnostic["measure_kind"] == "adapter_reconciliation"


# ---------------------------------------------------------------------------
# Exactly one provenance evidence basis per input -- payload fields
# (namespaced "<event_type>.<field>") use payload_field_status; bare envelope/
# structural fields use envelope_timestamp_presence or
# required_envelope_classification instead. Neither basis's companion key may
# appear on an input using the other.
# ---------------------------------------------------------------------------


def test_provenance_basis_missing_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_missing.json"))


def test_provenance_basis_invalid_value_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_invalid_value.json"))


def test_provenance_basis_envelope_field_as_payload_refused():
    """A structural/envelope field (event_type) cannot be passed off as a
    payload field_status."""
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_envelope_field_as_payload.json"))


def test_provenance_basis_payload_field_as_envelope_refused():
    """Vice versa: a genuine payload field cannot be passed off as a
    structural/envelope basis."""
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_payload_field_as_envelope.json"))


def test_provenance_basis_mixed_evidence_envelope_and_payload_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_mixed_evidence_envelope_and_payload.json"))


def test_provenance_basis_mixed_evidence_payload_and_envelope_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_mixed_evidence_payload_and_envelope.json"))


def test_provenance_basis_required_envelope_carries_status_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_required_envelope_carries_status.json"))


def test_provenance_basis_envelope_timestamp_missing_status_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_envelope_timestamp_missing_status.json"))


def test_provenance_basis_envelope_timestamp_bad_status_value_refused():
    """'inferred' is a payload-only vocabulary word -- a bare timestamp is
    either present or it isn't, there is no derivation concept for it."""
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_envelope_timestamp_bad_status_value.json"))


def test_provenance_basis_timestamp_under_classification_basis_refused():
    """Envelope bases are fixed to one specific bare field each, fail
    closed -- 'timestamp' cannot borrow required_envelope_classification just
    because both are bare envelope fields."""
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_timestamp_under_classification_basis.json"))


def test_provenance_basis_event_type_under_timestamp_basis_refused():
    """Vice versa: 'event_type' cannot borrow envelope_timestamp_presence."""
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(_load("invalid", "provenance_basis_event_type_under_timestamp_basis.json"))


def test_provenance_basis_arbitrary_bare_field_under_timestamp_basis_refused():
    """A bare field that is neither 'timestamp' nor 'event_type' (e.g.
    'sequence_index') cannot claim envelope_timestamp_presence either --
    the dotted-vs-bare rule alone is not sufficient, each envelope basis is
    fixed to its own one field."""
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(
            _load("invalid", "provenance_basis_arbitrary_bare_field_under_timestamp_basis.json")
        )


def test_provenance_basis_arbitrary_bare_field_under_classification_basis_refused():
    with pytest.raises(LocalDiscoveryProvenanceBasisInvalid):
        validate_report(
            _load("invalid", "provenance_basis_arbitrary_bare_field_under_classification_basis.json")
        )


def test_elapsed_event_span_uses_envelope_timestamp_basis():
    report = valid_report()
    inputs = report["results"]["elapsed_event_span"]["per_session"]["S1"]["provenance"]["inputs"]
    for inp in inputs:
        assert inp["basis"] == "envelope_timestamp_presence"
        assert inp["status"] in ("observed", "unavailable")
        assert "field_status" not in inp


def test_compaction_count_uses_required_envelope_classification_basis():
    report = valid_report()
    inputs = report["results"]["compaction_count"]["per_session"]["S1"]["provenance"]["inputs"]
    assert inputs == [{"field": "event_type", "basis": "required_envelope_classification"}]


def test_not_reportable_without_reason_refused_per_session():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "not_reportable_missing_reason_per_session.json"))


def test_aggregate_missing_rule_version_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "aggregate_missing_rule_version.json"))


def test_aggregate_not_reportable_missing_reason_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "aggregate_not_reportable_missing_reason.json"))


# ---------------------------------------------------------------------------
# Signal-vocabulary validation -- applies to per_session AND aggregate
# ---------------------------------------------------------------------------


def test_signal_outcome_against_empty_vocabulary_refused_per_session():
    with pytest.raises(LocalDiscoverySignalNotDeclared):
        validate_report(_load("invalid", "signal_vocab_violation_per_session.json"))


def test_signal_outcome_against_empty_vocabulary_refused_aggregate():
    with pytest.raises(LocalDiscoverySignalNotDeclared):
        validate_report(_load("invalid", "signal_vocab_violation_aggregate.json"))


def test_signal_outcome_not_in_declared_vocabulary_refused():
    report = valid_report()
    report["diagnostics"][3]["signal_vocabulary"] = ["elevated_compaction"]
    report["results"]["compaction_count"]["aggregate"] = {"outcome": "signal:something_else"}
    with pytest.raises(LocalDiscoverySignalNotDeclared):
        validate_report(report)


def test_signal_outcome_matching_declared_vocabulary_accepted():
    report = valid_report()
    report["diagnostics"][3]["signal_vocabulary"] = ["elevated_compaction"]
    report["results"]["compaction_count"]["per_session"]["S1"] = {
        "outcome": "signal:elevated_compaction"
    }
    # A per-session signal outcome is tallied under 'excluded' by this module's
    # documented convention (see validate_session_outcome_reconciliation).
    report["availability"]["compaction_count"]["sessions"] = {
        "considered": 5, "computed": 4, "not_reportable": 0, "excluded": 1
    }
    validate_report(report)  # must not raise


# ---------------------------------------------------------------------------
# Exclusive report creation, durable report-id registry, race/no-overwrite,
# safety, permissions, and index-state coverage
# ---------------------------------------------------------------------------


def _report_for(tmp_root: Path, report_id: str, filename: str | None = None) -> dict:
    report = valid_report()
    report["report_id"] = report_id
    report["report_path"] = str(tmp_root / (filename or f"{report_id}.json"))
    return report


def _index_status(root: Path, report_id: str) -> str:
    return json.loads((root / "_index" / report_id).read_text())["status"]


def test_create_report_writes_body_and_index_written(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    report = _report_for(root, "ldr-test-0001")

    written = create_report(report, root=root)

    assert written == Path(report["report_path"]).resolve()
    assert json.loads(written.read_text())["report_id"] == "ldr-test-0001"
    assert _index_status(root, "ldr-test-0001") == "written"


def test_create_report_refuses_duplicate_report_id_under_different_path(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    first = _report_for(root, "ldr-test-dup-id", filename="first.json")
    create_report(first, root=root)

    second = _report_for(root, "ldr-test-dup-id", filename="second.json")
    with pytest.raises(LocalDiscoveryDuplicateReport):
        create_report(second, root=root)
    assert not (root / "second.json").exists()
    assert _index_status(root, "ldr-test-dup-id") == "written"  # untouched by the failed attempt


def test_create_report_refuses_duplicate_report_path_under_different_id(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    shared_path = "shared.json"
    first = _report_for(root, "ldr-test-a", filename=shared_path)
    create_report(first, root=root)

    second = _report_for(root, "ldr-test-b", filename=shared_path)
    with pytest.raises(LocalDiscoveryDuplicateReport):
        create_report(second, root=root)
    assert json.loads((root / shared_path).read_text())["report_id"] == "ldr-test-a"
    # the second attempt's own id was reserved, then correctly marked failed -- not
    # left "reserved" forever, and never falsely "written"
    assert _index_status(root, "ldr-test-b") == "failed"


def test_create_report_concurrent_race_on_same_report_id_exactly_one_wins(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(filename: str) -> None:
        report = _report_for(root, "ldr-test-race-id", filename=filename)
        try:
            create_report(report, root=root)
            result = "ok"
        except LocalDiscoveryDuplicateReport:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(f"race-{i}.json",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("refused") == 7
    assert _index_status(root, "ldr-test-race-id") == "written"


def test_create_report_concurrent_race_on_same_report_path_different_ids(tmp_path):
    """Part C: concurrent writers with DIFFERENT report_ids all targeting the same
    report_path -- exactly one link succeeds, the rest are refused."""
    root = tmp_path / "tier1" / "local_discovery"
    shared_path = "race-shared.json"
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        report = _report_for(root, f"ldr-test-race-path-{i}", filename=shared_path)
        try:
            create_report(report, root=root)
            result = "ok"
        except LocalDiscoveryDuplicateReport:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("refused") == 7
    # every reserved id reaches a terminal state: exactly one "written", the rest "failed"
    statuses = [_index_status(root, f"ldr-test-race-path-{i}") for i in range(8)]
    assert statuses.count("written") == 1
    assert statuses.count("failed") == 7


def test_create_report_refuses_root_inside_repository_working_tree():
    inside_repo_root = REPO_ROOT / "local_discovery_scratch"
    report = _report_for(inside_repo_root, "ldr-test-repo-root")
    with pytest.raises(LocalDiscoveryPathError):
        create_report(report, root=inside_repo_root)
    assert not inside_repo_root.exists()


def test_create_report_refuses_report_path_outside_declared_root(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    other = tmp_path / "elsewhere"
    report = _report_for(other, "ldr-test-outside-root")
    with pytest.raises(LocalDiscoveryPathError):
        create_report(report, root=root)


def test_create_report_refuses_unsafe_report_id(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    report = _report_for(root, "ldr-safe-name")
    report["report_id"] = "../escape"
    report["report_path"] = str(root / "escape.json")
    with pytest.raises(LocalDiscoveryUnsafeReportId):
        create_report(report, root=root)
    # nothing was written anywhere, in particular not outside root
    assert not (root.parent / "escape.json").exists()
    assert not root.exists() or not any(root.iterdir())


def test_create_report_validates_before_writing_anything(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    report = _report_for(root, "ldr-test-invalid")
    report["scope"]["session_count"] = 999  # invalid: doesn't match input_sessions

    with pytest.raises(LocalDiscoveryScopeMismatch):
        create_report(report, root=root)

    assert not root.exists() or not any(root.iterdir())


def test_create_report_files_are_mode_0600(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    report = _report_for(root, "ldr-test-perms")

    written = create_report(report, root=root)

    for path in (written, root / "_index" / "ldr-test-perms"):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"{path} has mode {oct(mode)}, expected 0o600"


def test_create_report_failed_body_write_marks_index_failed_not_written(tmp_path):
    """A body-write/link failure must leave the index at 'failed', never silently
    'reserved' (looking incomplete-but-unexplained) or 'written' (looking successful
    when the body was never created at that id)."""
    root = tmp_path / "tier1" / "local_discovery"
    shared_path = "collide.json"
    create_report(_report_for(root, "ldr-first", filename=shared_path), root=root)

    with pytest.raises(LocalDiscoveryDuplicateReport):
        create_report(_report_for(root, "ldr-second", filename=shared_path), root=root)

    assert _index_status(root, "ldr-second") == "failed"
    assert _index_status(root, "ldr-first") == "written"


def test_default_root_matches_documented_location():
    assert DEFAULT_ROOT == Path.home() / ".agent-evidence-lab" / "local_discovery"


# ---------------------------------------------------------------------------
# A computed leaf must carry a value (per_session and aggregate)
# ---------------------------------------------------------------------------


def test_computed_missing_value_per_session_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "computed_missing_value_per_session.json"))


def test_computed_missing_value_aggregate_refused():
    with pytest.raises(LocalDiscoveryProvenanceInvalid):
        validate_report(_load("invalid", "computed_missing_value_aggregate.json"))


# ---------------------------------------------------------------------------
# An aggregate is required for every declared diagnostic in v1
# ---------------------------------------------------------------------------


def test_missing_aggregate_refused():
    with pytest.raises(jsonschema.exceptions.ValidationError):
        validate_report(_load("invalid", "missing_aggregate.json"))


# ---------------------------------------------------------------------------
# This v1 contract's fixed scope (one developer, one repo,
# Claude Code only) is enforced, not merely shape-checked
# ---------------------------------------------------------------------------


def test_scope_not_v1_subject_count_refused():
    with pytest.raises(LocalDiscoveryScopeNotSupported):
        validate_report(_load("invalid", "scope_not_v1_subject_count.json"))


def test_scope_not_v1_repository_count_refused():
    with pytest.raises(LocalDiscoveryScopeNotSupported):
        validate_report(_load("invalid", "scope_not_v1_repository_count.json"))


def test_scope_not_v1_source_client_refused():
    with pytest.raises(LocalDiscoveryScopeNotSupported):
        validate_report(_load("invalid", "scope_not_v1_source_client.json"))


# ---------------------------------------------------------------------------
# Tier 1 directories are private (0700), not just report files
# ---------------------------------------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_create_report_directories_are_mode_0700(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    report = _report_for(root, "ldr-test-dir-perms")

    create_report(report, root=root)

    assert _mode(root) == 0o700
    assert _mode(root / "_index") == 0o700
    assert _mode(Path(report["report_path"]).parent) == 0o700


def test_create_report_refuses_preexisting_group_readable_root(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    root.mkdir(parents=True, mode=0o750)
    os.chmod(root, 0o750)  # mkdir's mode is affected by umask; force it for the test
    report = _report_for(root, "ldr-test-loose-root")

    with pytest.raises(LocalDiscoveryPathError):
        create_report(report, root=root)


def test_create_report_refuses_preexisting_world_readable_index_dir(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    index_dir = root / "_index"
    index_dir.mkdir(mode=0o755)
    os.chmod(index_dir, 0o755)
    report = _report_for(root, "ldr-test-loose-index")

    with pytest.raises(LocalDiscoveryPathError):
        create_report(report, root=root)


def test_create_report_refuses_a_regular_file_occupying_the_root_path(tmp_path):
    """A permission-bit check alone would pass here (a plain file can be mode 0700
    too) and only fail later, indirectly, as NotADirectoryError -- _ensure_private_dir
    must check is_dir() explicitly."""
    root = tmp_path / "tier1" / "local_discovery"
    root.parent.mkdir(parents=True)
    root.write_text("not a directory")
    os.chmod(root, 0o700)
    report = _report_for(root, "ldr-test-root-is-a-file")

    with pytest.raises(LocalDiscoveryPathError):
        create_report(report, root=root)


def test_create_report_refuses_a_regular_file_occupying_the_index_path(tmp_path):
    root = tmp_path / "tier1" / "local_discovery"
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    index_dir = root / "_index"
    index_dir.write_text("not a directory")
    os.chmod(index_dir, 0o700)
    report = _report_for(root, "ldr-test-index-is-a-file")

    with pytest.raises(LocalDiscoveryPathError):
        create_report(report, root=root)


# ---------------------------------------------------------------------------
# Writes are complete, not vulnerable to a short os.write()
# ---------------------------------------------------------------------------


def test_write_all_loops_through_a_simulated_short_write(tmp_path, monkeypatch):
    import agent_evidence_lab.local_discovery_report as ldr_module

    real_write = os.write
    calls: list[int] = []

    def flaky_write(fd: int, data: bytes) -> int:
        # Simulate the OS accepting only the first byte of any write longer than one
        # byte -- a pathological but legal short write per the os.write() contract.
        calls.append(len(data))
        if len(data) > 1:
            return real_write(fd, data[:1])
        return real_write(fd, data)

    monkeypatch.setattr(ldr_module.os, "write", flaky_write)

    target = tmp_path / "short_write_target.txt"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        payload = b"0123456789" * 50  # long enough to force multiple short-write loops
        ldr_module._write_all(fd, payload)
    finally:
        os.close(fd)

    assert target.read_bytes() == payload
    assert len(calls) > 1  # confirms the short-write path was actually exercised


def test_write_all_refuses_a_zero_write_instead_of_hanging(tmp_path, monkeypatch):
    """os.write() returning 0 means no forward progress is possible -- looping on
    that would hang forever. _write_all must raise instead."""
    import agent_evidence_lab.local_discovery_report as ldr_module

    monkeypatch.setattr(ldr_module.os, "write", lambda fd, data: 0)

    target = tmp_path / "zero_write_target.txt"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with pytest.raises(LocalDiscoveryWriteError):
            ldr_module._write_all(fd, b"anything")
    finally:
        os.close(fd)


def test_create_report_survives_simulated_short_writes(tmp_path, monkeypatch):
    """End-to-end: create_report still produces a byte-perfect report even when the
    underlying os.write() only ever accepts a handful of bytes per call."""
    import agent_evidence_lab.local_discovery_report as ldr_module

    real_write = os.write

    def flaky_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:16] if len(data) > 16 else data)

    monkeypatch.setattr(ldr_module.os, "write", flaky_write)

    root = tmp_path / "tier1" / "local_discovery"
    report = _report_for(root, "ldr-test-short-write")

    written = create_report(report, root=root)

    on_disk = json.loads(written.read_text())
    assert on_disk["report_id"] == "ldr-test-short-write"
    assert on_disk == report
