---
name: ask-claude
description: Run a bounded Claude activity through the shared dispatcher or use the legacy pinned Opus review command.
---

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py status --activity ACTIVITY --config ROUTING` for a local account and priority check, then `provider_runner.py dispatch --activity ACTIVITY --provider claude --workspace WORKSPACE --task-file TASK --config ROUTING` to pin Claude within that activity. The activity decides read/search versus writing capabilities. Independent review starts fresh against the saved candidate and Jev diff binding; medium effort is the default, and high/deep review requires `--review-reason`. A confirmed account limit blocks Opus, Sonnet and Haiku together until reset or probe.

The legacy `review --provider claude` command remains pinned to Opus 5.5 for compatibility; its old fallback metadata is not the canonical activity chain. Never bypass unresolved ownership or shared quota state.
