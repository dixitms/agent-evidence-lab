# Synthetic raw-trace fixtures

These JSONL files are invented inputs for adapter tests. They model the
structural shapes an adapter must handle while containing no real prompt,
thinking content, tool argument, tool output, workspace path, or session
identifier.

The top-level files exercise the Claude Code adapter. They cover multi-block
assistant responses, tool-call/result pairing, explicit and unavailable result
states, compaction records, source ordering, prompt/turn edge cases, and
unknown raw shapes that must fail closed.

`managed_environment_claude/` contains a stricter profile of the same Claude
Code trace family. It exercises profile-specific privacy/disposition rules,
including records that must be preserved, records that must be rejected, and
fields that remain unavailable unless explicitly allowed.

`codex/` contains equivalent synthetic inputs for the Codex CLI adapter.

Run the adapter and fixture tests with:

```bash
pytest -q
```

## Privacy boundary

Real traces are never committed. These fixtures are shape-oriented
reconstructions with fabricated values, allowing the adapters to be tested and
shared without exporting private workspace data.
