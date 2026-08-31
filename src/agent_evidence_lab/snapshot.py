"""Immutable session snapshots.

Both clients' session files are **live**: one Claude Code session file grew
898 -> 1,074 lines while it was being read, and a second corpus gained records
between probes while its adapter was being written. Normalizing from one read
of a file and then reconciling from a second read of the same path silently
compares two different file states — and the reconciliation gate exists
precisely to catch silent divergence, so a gate that can be fooled that way is
worse than none.

A snapshot makes it mechanical rather than procedural: the bytes are read once,
hashed, and carried through both steps. `reconcile()` refuses to compare
against anything whose hash differs from the one that was normalized.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class SnapshotMismatch(Exception):
    """Reconciliation was attempted against different bytes than were normalized.

    Raised rather than warned. An append between normalize and reconcile
    invalidates the comparison, and a gate that reports "ok" on two different
    file states is exactly the failure mode this project has already been bitten
    by once.
    """


@dataclass(frozen=True)
class SessionSnapshot:
    """An immutable copy of one session file, plus its content hash."""

    source_path: Path
    content: bytes
    sha256: str

    @classmethod
    def capture(cls, path: str | Path) -> "SessionSnapshot":
        p = Path(path)
        data = p.read_bytes()
        return cls(source_path=p, content=data, sha256=hashlib.sha256(data).hexdigest())

    @classmethod
    def coerce(cls, source: "str | Path | SessionSnapshot") -> "SessionSnapshot":
        return source if isinstance(source, cls) else cls.capture(source)

    def lines(self) -> list[str]:
        return self.content.decode("utf-8").splitlines()

    def verify_same_as(self, expected_sha256: str) -> None:
        if self.sha256 != expected_sha256:
            raise SnapshotMismatch(
                f"{self.source_path}: file content changed between normalization and "
                f"reconciliation (normalized {expected_sha256[:12]}…, now "
                f"{self.sha256[:12]}…). Re-normalize from a single snapshot — a "
                f"reconciliation across two file states proves nothing."
            )
