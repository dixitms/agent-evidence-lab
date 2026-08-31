#!/usr/bin/env python3
"""Fail a prospective public release on common private-workspace residue.

This is intentionally a narrow hygiene gate, not a replacement for human
review. It accepts a candidate export directory and reports file paths and
rule names only; it never prints matched file content.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def _fragments(*parts: str) -> str:
    """Join pattern fragments.

    Every rule below is written as split fragments so that this file, which
    necessarily contains the patterns it searches for, does not match itself.
    A rule that flagged its own definition would make the gate unusable, and
    silencing that with a self-allowlist entry would make it circular instead.
    """
    return "".join(parts)


# Markdown filenames written in upper snake case are this project's convention
# for private working documents (working plans, frozen protocols, proposals,
# review packets). Public documents are lower kebab case, plus README.
_PRIVATE_DOC_FILENAME = _fragments(
    r"\b(?!READ", r"ME\b)[A-Z][A-Z0-9_]{2,}\.", r"md\b"
)
_PRIVATE_DOC_NAMED = _fragments(
    r"\b(?:CLA", r"UDE|JOUR", r"NAL|HAND", r"OFF|CHART", r"ER|DES", r"IGN|ROAD", r"MAP)\.", r"md\b"
)

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "home_path",
        re.compile(r"(?:/" + r"Users/|/" + r"home/|\\\\" + r"Users\\|[A-Za-z]:\\" + r"Users\\)"),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("private_laptop_reference", re.compile(r"\b(?:personal|" + r"corpor" + r"ate) laptop\b", re.I)),
    # No trailing word boundary: the term also has to be caught inside a
    # CamelCase identifier, which is how it survived an earlier sweep.
    ("corpor" + "ate_terminology", re.compile(r"\b" + r"corpor" + r"ate", re.I)),
    (
        "reviewer_directed_language",
        re.compile(
            r"\b(?:under "
            + r"review|await" + r"ing|say the " + r"word|for your (?:codex|claude) session)\b",
            re.I,
        ),
    ),
    (
        "private_document_reference",
        re.compile(_PRIVATE_DOC_FILENAME + "|" + _PRIVATE_DOC_NAMED),
    ),
    (
        "internal_review_vocabulary",
        re.compile(
            _fragments(
                r"\bro", r"und [0-9]+\b",
                r"|\b[0-9]+(?:st|nd|rd|th)[- ]pa", r"ss\b",
                r"|\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
                r"[- ]pa", r"ss\b",
                r"|\brev", r"iew packet\b",
                r"|\bpre", r"registration deviation\b",
            ),
            re.I,
        ),
    ),
    (
        # Case-sensitive on purpose. The internal milestone codes are always
        # written in caps, whereas the lowercase forms are ordinary content:
        # a synthetic message identifier in the fixtures, or the verb "stage".
        "internal_milestone_code",
        re.compile(
            _fragments(r"\bSt", r"age [A-G][0-9]?\b|\bM", r"3\b|\bF[2-5]\b|\bG[1-6]\b")
        ),
    ),
    (
        "foreign_project_reference",
        re.compile(_fragments(r"\bCont", r"ext", r"Guard\b"), re.I),
    ),
    (
        # A public contract must not cite a numbered section of a document the
        # release does not ship. State the rule instead of pointing at it.
        "orphaned_section_reference",
        re.compile(_fragments(r"§\s?[0-9]|", r"\\\\u00a7[0-9]")),
    ),
    (
        "credential_material",
        re.compile(
            _fragments(
                r"\bsk-", r"ant-api[0-9]",
                r"|\bgh[pousr]_", r"[A-Za-z0-9]{16,}",
                r"|\bAK", r"IA[0-9A-Z]{16}\b",
                r"|\bBea", r"rer [A-Za-z0-9._~+/-]{16,}",
                r"|-----BE", r"GIN [A-Z ]*PRI", r"VATE KEY-----",
            )
        ),
    ),
)

FORBIDDEN_PATH_PARTS = frozenset({".git", ".secrets", "reviews"})
IGNORED_RUNTIME_PATH_PARTS = frozenset(
    {".mypy_cache", ".public-release", ".pytest_cache", ".venv", "__pycache__", "venv"}
)
FORBIDDEN_FILENAMES = frozenset(
    {_fragments("JOURNAL.", "md"), _fragments("HANDOFF.", "md")}
)


def load_allowlist(path: Path | None) -> frozenset[tuple[str, str]]:
    """Load narrowly-scoped synthetic-security-test exceptions."""
    if path is None:
        return frozenset()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid hygiene allowlist: {path}") from exc
    if not isinstance(entries, list):
        raise ValueError("hygiene allowlist must be a JSON array")
    allowed: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "rules", "reason"}:
            raise ValueError("each hygiene allowlist entry needs path, rules, and reason")
        path_value, rules, reason = entry["path"], entry["rules"], entry["reason"]
        if not isinstance(path_value, str) or not path_value or not isinstance(reason, str) or not reason:
            raise ValueError("allowlist path and reason must be non-empty strings")
        if not isinstance(rules, list) or not rules or not all(isinstance(rule, str) for rule in rules):
            raise ValueError("allowlist rules must be a non-empty string array")
        allowed.update((rule, path_value) for rule in rules)
    return frozenset(allowed)


def walk_release(root: Path) -> tuple[list[Path], list[Path]]:
    """Return candidate files and forbidden paths, pruning excluded trees."""
    files: list[Path] = []
    forbidden: list[Path] = []
    for directory, child_dirs, filenames in os.walk(root):
        directory_path = Path(directory)
        kept_dirs: list[str] = []
        for child_dir in child_dirs:
            child_path = directory_path / child_dir
            # A release candidate may legitimately be a freshly initialized
            # repository. Its root metadata is neither payload nor a copied
            # private artifact, so do not scan or flag it. A nested .git is a
            # different repository embedded in the release and remains a hard
            # refusal.
            if child_dir == ".git" and directory_path == root:
                continue
            if child_dir in FORBIDDEN_PATH_PARTS:
                forbidden.append(child_path)
            elif child_dir in IGNORED_RUNTIME_PATH_PARTS:
                continue
            else:
                kept_dirs.append(child_dir)
        child_dirs[:] = kept_dirs
        files.extend(directory_path / filename for filename in filenames)
    return sorted(files), sorted(forbidden)


def audit(root: Path, allowlist: frozenset[tuple[str, str]] = frozenset()) -> list[tuple[str, Path]]:
    findings: list[tuple[str, Path]] = []
    files, forbidden_paths = walk_release(root)
    for path in forbidden_paths:
        findings.append(("forbidden_path", path.relative_to(root)))
    for path in files:
        relative = path.relative_to(root)
        if path.is_file() and path.name in FORBIDDEN_FILENAMES:
            findings.append(("forbidden_filename", relative))

    for path in files:
        relative = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for rule_name, pattern in RULES:
            if pattern.search(text) and (rule_name, str(relative)) not in allowlist:
                findings.append((rule_name, relative))
    return sorted(set(findings), key=lambda item: (item[0], str(item[1])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_root", type=Path, help="candidate public-release directory")
    parser.add_argument(
        "--allowlist",
        type=Path,
        help="JSON list of explicit synthetic-test exceptions; omitted means no exceptions",
    )
    args = parser.parse_args()
    root = args.release_root.resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    try:
        findings = audit(root, load_allowlist(args.allowlist))
    except ValueError as exc:
        parser.error(str(exc))
    if not findings:
        print("public-release hygiene audit: PASS")
        return 0
    print("public-release hygiene audit: FAIL", file=sys.stderr)
    for rule_name, relative in findings:
        print(f"{rule_name}: {relative}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
