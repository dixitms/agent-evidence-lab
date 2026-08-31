# Synthetic fixtures

This directory contains hand-written, synthetic JSON records used to test the
versioned schemas and validators.

`canonical/valid/` contains records expected to validate. `canonical/invalid/`
contains one-rule mutations expected to fail. `cases.json` names each case, its
schema version, and its expected outcome. The fixture runner also generates
payload-exclusivity combinations from the schema so that a newly added payload
type cannot silently weaken an existing rule.

Run the full suite with:

```bash
pytest -q
```

## Privacy boundary

These fixtures contain no real transcript content. Their values are invented,
including paths, digests, and session-like references. They exist to test data
shape and availability handling—for example, an explicitly unavailable tool
result state or a synthesized session boundary—not to reproduce a real
developer session.
