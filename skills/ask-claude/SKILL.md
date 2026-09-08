---
name: ask-claude
description: Run independent read-only code review through the installed Claude CLI using Opus 5, with evidence-backed findings and no Sonnet or Fable routing.
---

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py` with Python 3.11 or newer (`python3` on macOS; `python` on Windows). Read `{{CODEX_ROOT}}/agent-framework/docs/FRAMEWORK.md` for the contract and command. Invoke role `review` in the active workspace. Pin `claude-opus-5`; never substitute the rolling opus alias, Sonnet or Fable. Do not use provider fallback flags or spawn Claude subagents. The Claude invocation supports only medium or high effort and remains pinned to Opus 5; the separate native fallback remains available.

Routine reviews explicitly default to `--effort medium`. For complex reviews involving coupled contracts, state machines, concurrency, security, or unresolved findings, supply `--review-effort high` with a non-empty `--review-reason`. Never raise review effort just due to diff size; large changes without architectural coupling remain medium effort.

Provide the integrated batch contract, exact candidate HEAD, base revision and saved diff path with relevant source/test paths. Start fresh, never resume the implementer's conversation. The adapter uses the user-authorized CLI bypass mode but limits tools to reading/search, disables customizations, excludes shell execution and MCP tools, and does not grant edits. Supply applicable instructions explicitly. Do not weaken these controls to get a review through.

Ask for severity, file/line, failure scenario and suggested correction for each material finding, or an explicit no-findings result with validation gaps. Treat findings as evidence for the primary to assess. On Claude authentication, availability or quota failure, a fresh native `reviewer` on `gpt-6-astra` / `low` is the permitted fallback; disclose it. Never replace Opus 5 with Sonnet or Fable.

Use this skill when independent review adds meaningful confidence or is explicitly required; small, well-understood fixes may use focused checks and self-review. Track material findings and repairs concisely. Re-review a repair or add a scoped Sol-high correctness gate when a concrete unresolved risk warrants it, rather than automatically. Review is not execution evidence; the primary may run checks directly and reuse applicable evidence without a fresh verifier.

## Concurrent reviews

Independent Claude reviews may run concurrently, including in the same workspace. The adapter does not impose the Gemini ownership gates on read-only reviews. Provide a stable candidate and distinct task contracts so each review has clear scope.
