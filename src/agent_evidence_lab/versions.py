"""Supported format versions, enforced centrally.

`manifest_version`, `preregistration_version` and `bundle_version` are contracts,
not labels. A version string nobody checks is decoration: the point of stamping
a format is that a consumer can *refuse* one it does not understand, rather than
reading a 2.0.0 document with 1.0.0 assumptions and producing something that
looks fine.

Every check here fails closed. Adding a supported version is a deliberate edit
to this table, which is exactly the friction a format change should have.
"""

from __future__ import annotations

import re

SUPPORTED: dict[str, frozenset[str]] = {
    "manifest_version": frozenset({"1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"}),
    "preregistration_version": frozenset({"1.0.0"}),
    "bundle_version": frozenset({"1.0.0"}),
    # 1.0.0 stays readable though nothing ever wrote it; 2.0.0 is what is written.
    "ledger_version": frozenset({"1.0.0", "2.0.0"}),
}

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class UnsupportedFormatVersion(Exception):
    """A document declares a format version this build does not support."""


def version_failure(kind: str, value: object) -> str | None:
    """Return a refusal reason, or None if the version is supported.

    Used by validators that collect every reason rather than raising on the
    first; `require_version` is the raising form.
    """
    if kind not in SUPPORTED:  # pragma: no cover - programming error, not data
        raise KeyError(f"unknown version kind {kind!r}")
    if value is None or value == "":
        return f"{kind} is missing — an unversioned document cannot be checked"
    if not isinstance(value, str) or not _SEMVER.match(value):
        return f"{kind}={value!r} is not a semantic version"
    if value not in SUPPORTED[kind]:
        return (
            f"{kind}={value!r} is not supported by this build "
            f"(supported: {sorted(SUPPORTED[kind])}) — refusing rather than reading "
            f"it with assumptions from another version"
        )
    return None


def require_version(kind: str, value: object) -> None:
    reason = version_failure(kind, value)
    if reason is not None:
        raise UnsupportedFormatVersion(reason)
