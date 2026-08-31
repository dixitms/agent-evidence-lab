#!/usr/bin/env python3
"""Schema-validation runner (all live contract versions).

Validates every fixture in fixtures/cases.json against the schema version
that fixture declares — 0.1.1 in schema/v0.1 for Claude Code, 0.2.0 in
schema/v0.2 for Codex — and asserts each one passes or fails as the manifest
says it should. Versions are checked separately and never pooled.

A case that is *supposed* to fail but validates cleanly is reported as a
failure here — that is the whole point: it proves the schema's conditionals
actually reject what the schemas document they reject. The generated exclusivity
matrix runs against every live agent_event version, so a new version cannot
ship a weaker guarantee than the one before it.

This is NOT an adapter and NOT a parser. It reads no client transcript and
normalizes nothing; it only checks hand-written canonical records against the
schema files.

Usage:  python3 fixtures/validate_fixtures.py
Deps:   pip install -r requirements-dev.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency guidance, not logic
    sys.exit(
        "jsonschema is not installed.\n"
        "Install the declared dev dependency:  pip install -r requirements-dev.txt"
    )

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "fixtures" / "cases.json"


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    validators = {
        name: Draft202012Validator(json.loads((ROOT / rel).read_text()))
        for name, rel in manifest["schemas"].items()
    }

    passed: list[str] = []
    failed: list[str] = []

    for case in manifest["cases"]:
        fixture_path = ROOT / "fixtures" / "canonical" / case["fixture"]
        instance = json.loads(fixture_path.read_text())
        errors = sorted(
            validators[case["schema"]].iter_errors(instance), key=lambda e: e.path
        )
        actually_valid = not errors
        expected_valid = case["expect"] == "valid"

        if actually_valid == expected_valid:
            passed.append(case["fixture"])
        else:
            failed.append(case["fixture"])
            print(f"FAIL  {case['fixture']}")
            print(f"      expected: {case['expect']}")
            if case.get("reason"):
                print(f"      case:     {case['reason']}")
            if expected_valid:
                for err in errors:
                    loc = "/".join(str(p) for p in err.path) or "<root>"
                    print(f"      error at {loc}: {err.message}")
            else:
                print("      but the schema accepted it — the rule is not enforced")

    file_total = len(passed) + len(failed)
    by_ver: dict[str, int] = {}
    for case in manifest["cases"]:
        by_ver[case.get("schema_version", "?")] = by_ver.get(case.get("schema_version", "?"), 0) + 1
    print(f"{len(passed)}/{file_total} fixture cases behaved as declared "
          f"({', '.join(f'{v}: {n}' for v, n in sorted(by_ver.items()))}).")

    m_passed, m_failed = 0, []
    for schema_name in manifest["payload_exclusivity_matrix"].get(
        "applies_to_schemas", ["agent_event"]
    ):
        sv = manifest["schemas"][schema_name]
        p_, f_ = exclusivity_matrix(manifest, validators[schema_name], schema_name)
        print(f"{p_}/{p_ + len(f_)} payload-exclusivity combinations rejected "
              f"as required ({schema_name}: {sv})")
        m_passed += p_
        m_failed += f_
    for combo in m_failed:
        print(f"FAIL  {combo}")

    total = file_total + m_passed + len(m_failed)
    ok = len(passed) + m_passed
    print(f"\n{ok}/{total} schema-validation checks behaved as declared.")
    if failed or m_failed:
        if failed:
            print(f"{len(failed)} fixture case(s) did not: " + ", ".join(failed))
        if m_failed:
            print(f"{len(m_failed)} exclusivity combination(s) were wrongly accepted.")
        return 1
    return 0


def exclusivity_matrix(manifest, validator, schema_name="agent_event") -> tuple[int, list[str]]:
    """Assert no event_type accepts a payload belonging to another event_type.

    Built from the schema's own type list rather than from stored files, so a
    newly added payload type is covered the day it appears. Hand-maintained
    exclusivity lists drifted twice during development; a generated matrix
    cannot drift the same way.
    """
    spec = manifest["payload_exclusivity_matrix"]
    # Version-specific envelope facts. Probe instances must be valid apart from
    # the payload under test, or the matrix would "pass" for the wrong reason.
    version = "0.2.0" if schema_name.endswith("_v02") else "0.1.1"
    with_payload = spec["payload_event_types"]
    without = spec["no_payload_event_types"]

    # Minimal payload bodies — only enough to be structurally present.
    bodies = {
        "tool_call": {"tool_name": "Read"},
        "tool_result": {"result_size_value": 1, "result_size_unit": "utf8_bytes",
                        "result_size_method": "direct"},
        "thinking": {"length_value": 1},
        "compaction": {"compaction_source": "boundary_marker"},
        "assistant_message": {"output_tokens": 1},
        "user_message": {"prompt_source": "typed"},
    }

    passed, failed = 0, []
    for event_type in with_payload + without:
        own = bodies.get(event_type) if event_type in with_payload else None
        for foreign, body in bodies.items():
            if foreign == event_type:
                continue
            instance = {
                "schema_version": version,
                "event_id": "fdd49503-8b02-5de0-a40e-c57617f1d2ef",
                "sequence_index": 0,
                "run_id": "6f1d3a2e-0c4b-4f8a-9e21-5b7c8d9e0a11",
                "source_client": "copilot-vscode",
                "session_id": "sha256:fixture",
                "repo_ref": "sha256:fixture",
                "turn_index": 0,
                "event_type": event_type,
                "event_origin": "observed",
                "field_status": {"probe": "observed"},
                foreign: body,
            }
            if own is not None:
                instance[event_type] = own
            if validator.is_valid(instance):
                failed.append(
                    f"[{schema_name}] event_type '{event_type}' wrongly accepted "
                    f"a '{foreign}' payload"
                )
            else:
                passed += 1
    return passed, failed


if __name__ == "__main__":
    raise SystemExit(main())
