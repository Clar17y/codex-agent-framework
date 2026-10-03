---
name: jev-search
description: Find code by behaviour with Jev; mandatory at the start of substantial coding investigations before delegation, returning compact original source evidence.
---

Use the Python 3.11+ helper at `{{CODEX_ROOT}}/agent-framework/scripts/jev_search.py`. Running `jev-search` is mandatory at the start of any substantial coding investigation or architectural exploration before delegating implementation. The helper collects and scores source internally; do not first print a broad grep result or paste a repository into the conversation.

Concise permitted exemptions:
- Exact symbol, identifier, or literal search where location is already known.
- Tiny localized single-site fixes, documentation-only changes, or configuration edits where behavioral search is not applicable.
When claiming an exemption, record a concise, non-empty reason.

```text
python "{{CODEX_ROOT}}/agent-framework/scripts/jev_search.py" search --workspace "<absolute workspace>" --query "Where are overlapping writers prevented?" --scope scripts --top-k 6 --config "{{CODEX_ROOT}}/agent-framework/routing.json"
```

Use `python3` where required. `doctor` and `inspect` are offline diagnostics (not search evidence). `--scope` can repeat; start with relevant directories and broaden only when evidence or incomplete coverage warrants it. `--query-file` supports longer questions. Keep the returned context budget small and read the cited files in detail as needed.

Remote evaluation uses `TYPESAFE_API_KEY` and the saved `capabilities.jev` settings. Jev defaults to `enabled: true` with `authorization_mode: "all_workspaces"`, authorizing ordinary use across coding repositories and Git worktrees without per-workspace prompts or `--allow-remote`. Honor explicit opt-outs and `allowed_roots` mode restrictions, including legacy root lists without a mode. Diagnostics expose key presence, never its value.

A denied or unavailable attempt is not a session-wide opt-out. Reassess later applicable work and retry after configuration, credentials, workspace, or service readiness changes; do not carry unavailable evidence forward as successful preparation. Keep retries bounded and continue locally while an explicit opt-out or missing prerequisite persists.

Treat relevance as a search signal. Original excerpts and their source locations are the evidence. Keep partial coverage, skipped files, unknown scores and stale sources visible. Empty results do not establish absence. On missing credentials, unavailable service, or provider failure, retain the diagnostics, use the explicitly unscored local candidates, and continue local investigation; do not abort work and do not mistake fallback order for Jev scores.

The primary agent or native explorer prepares this remote Jev context before external delegation; Gemini and Claude execution children deliberately do not inherit the Jev key, and Claude's read/search-only review restrictions remain intact.

When `workflow.require_jev_evidence=true` in `routing.json`, delegated implementation tasks through `provider_runner.py` mechanically require a valid `task.jev.search` accounting block (`attempted` with a bounded workspace result path or `not_applicable` with a concise reason). Instruction-level policy governs arbitrary native subagent calls.

See `{{CODEX_ROOT}}/agent-framework/docs/JEV.md` for configuration, budgets, cache semantics and evaluation limits.
