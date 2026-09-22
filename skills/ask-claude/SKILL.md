---
name: ask-claude
description: Run independent read-only code review through the installed Claude CLI using Opus 5.5, with evidence-backed findings and no Sonnet or Fable routing.
---

Claude Opus 5.5 requires Claude Code 2.1.280 or later. Check the configured executable's `--version` when diagnosing model availability; `claude update` reports the appropriate upgrade method. Preserve provider-wide quota evidence during model upgrades.

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py` with Python 3.11 or newer (`python3` on macOS; `python` on Windows). Read `{{CODEX_ROOT}}/agent-framework/docs/FRAMEWORK.md` for the contract and command. Invoke role `review` in the active workspace. Pin `claude-opus-5-5`; never substitute the rolling opus alias, Sonnet or Fable. Do not use provider fallback flags or spawn Claude subagents. The Claude invocation supports only medium or high effort and remains pinned to Opus 5.5; the separate native fallback remains available.

Before preparing or launching a review, run `python "{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py" status --provider claude --config "{{CODEX_ROOT}}/agent-framework/routing.json"` (use `python3` on macOS). This local check needs no task/workspace and never contacts Claude. On exit 20 / `fallback_required`, the provider-first check is satisfied: immediately use a fresh native `reviewer`, `gpt-6-astra` / `low`, and disclose cached exhaustion; do not launch the Claude CLI for each operation. On `state_error`, disclose the state-read failure and use the same fallback, preserving the state for investigation. Only `available_to_try` permits a provider attempt; it does not guarantee remaining usage. Always use the installed config so separate workspaces share `state/claude-quota.json`; do not bypass the cache with a different state directory.

The normal `review` command enforces this cache too and returns the same Astra fallback on a new confirmed terminal usage-limit response. It records a known reset when unambiguous, otherwise a bounded probe cooldown. After expiry, the next review may attempt Claude again; do not poll the provider while blocked. User-reported exhaustion may be recorded with `quota-set --provider claude --config "{{CODEX_ROOT}}/agent-framework/routing.json" --reason "User-reported exhaustion"`, optionally with `--reset-at` containing a future ISO timestamp and timezone. Omit the reset if unknown; a cooldown is not a claim that usage resets then. Auth/setup failures and transient throttling must not be recorded as exhausted usage.

Routine reviews explicitly default to `--effort medium`. For complex reviews involving coupled contracts, state machines, concurrency, security, or unresolved findings, supply `--review-effort high` with a non-empty `--review-reason`. Never raise review effort just due to diff size; large changes without architectural coupling remain medium effort.

Provide the integrated batch contract, exact candidate HEAD, base revision and saved diff path with relevant source/test paths. Start fresh, never resume the implementer's conversation. The adapter uses the user-authorized CLI bypass mode but limits tools to reading/search, disables customizations, excludes shell execution and MCP tools, and does not grant edits. Supply applicable instructions explicitly. Do not weaken these controls to get a review through.

Ask for severity, file/line, failure scenario and suggested correction for each material finding, or an explicit no-findings result with validation gaps. Treat findings as evidence for the primary to assess. On Claude authentication, availability or quota failure, a fresh native `reviewer` on `gpt-6-astra` / `low` is the permitted fallback; disclose it. Never replace Opus 5.5 with Sonnet or Fable.

Use this skill when independent review adds meaningful confidence or is explicitly required; small, well-understood fixes may use focused checks and self-review. Track material findings and repairs concisely. Re-review a repair or add a scoped Sol-high correctness gate when a concrete unresolved risk warrants it, rather than automatically. Review is not execution evidence; the primary may run checks directly and reuse applicable evidence without a fresh verifier.

## Concurrent reviews

Independent Claude reviews may run concurrently, including in the same workspace. The adapter does not impose the Gemini ownership gates on read-only reviews. Provide a stable candidate and distinct task contracts so each review has clear scope.

## Lifecycle telemetry

Claude uses `stream-json` telemetry (`--output-format stream-json --verbose`): the adapter incrementally reads NDJSON events from stdout, emits throttled compact step/tool transitions on stderr, and atomically writes `progress.json`. `heartbeat.json` serves as the quiet-period liveness fallback (default 60s). This adds no Claude turns, extra summaries, or Claude-token cost; compact stderr summaries add a small, bounded amount to the parent agent's context. The snapshot records bounded metadata (events, tool name, step state, normalized usage counters) but strictly excludes prompts, model text, tool parameters, and tool outputs. Keep the adapter command session open instead of using detached polling loops. A live heartbeat or recent stream event is diagnostic evidence, never proof of task correctness or eventual completion. Native subagents continue to use completion notifications and do not need periodic prose-progress files.
