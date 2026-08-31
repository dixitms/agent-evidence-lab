# Synthetic Codex CLI raw fixtures

This directory contains invented rollout-style JSONL inputs for the Codex CLI
adapter. The fixtures test structural behaviour such as message projections,
tool calls/results, opaque reasoning, compaction, source metadata, and unknown
subtypes.

They also cover failure modes the adapter must refuse rather than approximate:
a missing repository anchor, an unrecognised event subtype, and a source file
that changes while it is being reconciled.

Every value is synthetic. No committed fixture is copied from a Codex session
or contains real prompt, source, tool, or user data.

Run the tests with:

```bash
pytest -q
```
