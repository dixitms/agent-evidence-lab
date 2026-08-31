"""Tests for the prospective-public-release hygiene gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_AUDIT_PATH = Path(__file__).resolve().parents[1] / "tools" / "public_release_audit.py"
_SPEC = importlib.util.spec_from_file_location("public_release_audit", _AUDIT_PATH)
assert _SPEC and _SPEC.loader
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


def test_root_git_metadata_is_ignored_but_nested_git_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "embedded").mkdir()
    (tmp_path / "embedded" / ".git").mkdir()

    findings = _AUDIT.audit(tmp_path)

    assert findings == [("forbidden_path", Path("embedded/.git"))]
