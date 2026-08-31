from __future__ import annotations

from pathlib import Path

from agent_evidence_lab.structural_inventory import InventorySource, build_inventory, discover_sources

ROOT = Path(__file__).resolve().parent.parent


def test_inventory_is_cohort_separated_and_aggregate_only():
    report = build_inventory([
        InventorySource(ROOT / "fixtures/raw/session_shapes.jsonl", "claude-code", ROOT),
        InventorySource(ROOT / "fixtures/raw/codex/session_shapes.jsonl", "codex-cli"),
    ])
    rows = report.to_dict()["cohorts"]
    assert {(r["source_client"], r["schema_version"]) for r in rows} == {
        ("claude-code", "0.1.1"), ("codex-cli", "0.2.0")}
    assert all(r["session_count"] == r["normalized_sessions"] == r["reconciled_sessions"] == 1
               for r in rows)
    serialized = str(report.to_dict())
    assert "session_shapes.jsonl" not in serialized
    assert "local_path" not in serialized


def test_inventory_reports_adapter_failures_without_path_or_raw_text(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"unknown"}\n')
    report = build_inventory([InventorySource(bad, "claude-code", tmp_path)])
    row = report.to_dict()["cohorts"][0]
    assert row["session_count"] == 1
    assert row["normalized_sessions"] == row["reconciled_sessions"] == 0
    assert row["failures"] == {"UnrecognizedRawType": 1}
    assert str(bad) not in str(report.to_dict())


def test_inventory_snapshot_id_is_stable_and_changes_with_input(tmp_path):
    source = ROOT / "fixtures/raw/session_shapes.jsonl"
    first = build_inventory([InventorySource(source, "claude-code", ROOT)]).to_dict()
    second = build_inventory([InventorySource(source, "claude-code", ROOT)]).to_dict()
    assert first == second
    changed = tmp_path / "changed.jsonl"
    changed.write_bytes(source.read_bytes() + b"\n")
    third = build_inventory([InventorySource(changed, "claude-code", ROOT)]).to_dict()
    assert first["cohorts"][0]["cohort_snapshot_sha256"] != third["cohorts"][0]["cohort_snapshot_sha256"]


def test_discovery_is_client_scoped_and_does_not_require_content(tmp_path):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    (claude / "project").mkdir(parents=True)
    (codex / "2026/08/21").mkdir(parents=True)
    (claude / "project/a.jsonl").write_text("")
    (codex / "2026/08/21/b.jsonl").write_text("")
    sources = discover_sources(claude_root=claude, codex_root=codex)
    assert [(s.path.name, s.source_client) for s in sources] == [
        ("a.jsonl", "claude-code"), ("b.jsonl", "codex-cli")]
