---
name: route-activity
description: Route a bounded coding activity across Codex, Claude, Gemini and Muse using editable subscription priority chains and shared ownership.
---

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py` with the installed `routing.json`. Read [FRAMEWORK.md]({{CODEX_ROOT}}/agent-framework/docs/FRAMEWORK.md) for contract fields, activity names and command examples.

Check `status --activity ACTIVITY --config ROUTING` locally, then run `dispatch --activity ACTIVITY --workspace WORKSPACE --task-file TASK --config ROUTING`. The dispatcher chooses an ordered profile from `routing.json`. Use `--profile NAME` or `--provider NAME` only for an explicit override. The host primary model stays as selected. Each writing task needs explicit nonoverlapping owned paths. Static roles get enforced read/search capabilities and fresh context. Review uses a stable candidate and Jev diff binding; high/deep review needs `--review-reason`.

The CLI agent selected for orchestration needs a task with `allow_delegation: true`. It calls this same dispatcher for bounded children, waits for their result JSON and checks, then integrates evidence. The orchestrator does not claim the whole workspace. Ordinary workers do not recursively delegate without contract authorization.

One provider quota cache covers all its models and activities. A known terminal failure may advance to the next route only after a writer claim is resolved. A timeout, cancellation or malformed terminal blocks overlapping fallback. Preserve partial edits and report the pending path; never clear it merely to retry. `all_routes_unavailable` means the task remains unfinished.
