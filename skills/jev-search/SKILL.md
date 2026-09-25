---
name: jev-search
description: Find code by behaviour with Jev when identifiers are unknown or local search produces too much context; return compact original source evidence.
---

Use the Python 3.11+ helper at `{{CODEX_ROOT}}/agent-framework/scripts/jev_search.py`. The helper collects and scores source internally; do not first print a broad grep result or paste a repository into the conversation. Use ordinary exact search when the identifier is known or a few local matches already answer the question.

```text
python "{{CODEX_ROOT}}/agent-framework/scripts/jev_search.py" search --workspace "<absolute workspace>" --query "Where are overlapping writers prevented?" --scope scripts --top-k 6 --config "{{CODEX_ROOT}}/agent-framework/routing.json"
```

Use `python3` where required. `doctor` and `inspect` are offline diagnostics. `--scope` can repeat; start with relevant directories and broaden only when evidence or incomplete coverage warrants it. `--query-file` supports longer questions. Keep the returned context budget small and read the cited files in detail as needed.

Remote evaluation uses `TYPESAFE_API_KEY` and the saved `capabilities.jev` settings. A key's presence alone does not enable it. Saved `enabled: true` plus an exact workspace entry in `allowed_roots` authorizes repeated use there. Use `--allow-remote` for an individual invocation only when the user has already authorized sending this workspace's eligible excerpts to TypeSafe. Do not re-ask within that authorization or silently authorize unrelated workspaces. Diagnostics expose key presence, never its value.

Treat relevance as a search signal. Original excerpts and their source locations are the evidence. Keep partial coverage, skipped files, unknown scores and stale sources visible. Empty results do not establish absence. On missing credentials or provider failure, use the explicitly unscored local candidates and continue local investigation; do not mistake fallback order for Jev scores.

The primary or native explorer prepares this context before external delegation. Pass only relevant evidence into worker contracts. Gemini and Claude execution children deliberately do not inherit the Jev key; Claude's read/search-only review restrictions remain intact.

See `{{CODEX_ROOT}}/agent-framework/docs/JEV.md` for configuration, budgets, cache semantics and evaluation limits.
