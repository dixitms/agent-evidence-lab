# Claude Code guidance

Agent Policy Lab is a local-first reference implementation for analysing
coding-agent traces without turning raw developer activity into a monitoring
dataset or performance score.

## Non-negotiable boundaries

- Treat raw traces, prompts, tool arguments, tool output, workspace paths,
  secrets, and local stores as Tier 1 data. Do not add them to the repository,
  fixtures, logs, screenshots, or generated documentation.
- Committed fixtures must remain synthetic. When adding a fixture, use
  invented identifiers, paths, messages, and values.
- Preserve the distinction between `observed`, `inferred`, and `unavailable`.
  Do not fill a missing client field with a plausible substitute.
- Keep export validation fail-closed. Do not add bypass, force, skip-check, or
  partial-export modes.
- A canonical event can bound a review question; it must not be used to claim
  intent or task quality that requires protected context.

## Working conventions

- Prefer small, separately testable changes.
- Update schemas, adapters, fixtures, and tests together when a contract
  changes.
- Preserve reconciliation: an adapter must account for every supported raw
  record class or explicitly preserve/report why it did not normalize it.
- Do not pool clients, schema versions, or availability states as though they
  were equivalent.
- Keep public documentation understandable without access to local traces or
  private planning material.

## Verification

Run the relevant focused tests while editing, then run:

```bash
pytest -q
python3 tools/repository_hygiene_audit.py .
```

The audit must pass before preparing a public release.
