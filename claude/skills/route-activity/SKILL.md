---
name: route-activity
description: Delegate a bounded coding activity (implementation, testing, verification, review, exploration, documentation and others) across Codex, Gemini, Muse and Claude using the installed priority chains, quota state and file ownership.
---

Use `{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py` with `{{CLAUDE_ROOT}}/agent-framework/routing.json`. Read `{{CLAUDE_ROOT}}/agent-framework/docs/FRAMEWORK.md` for contract fields and recovery, and `{{CLAUDE_ROOT}}/agent-framework/docs/CLAUDE_HOST.md` for the Claude Code workflow.

Activities: `planning`, `exploration`, `implementation`, `implementation_complex`, `implementation_long_horizon`, `test_design`, `testing`, `verification`, `independent_review`, `security_review`, `refactor_audit`, `documentation`, `docs_research`, `deep_review`, `orchestration`.

1. Scope the work and write a compact contract from `task-template.json` into the workspace's `.llm-output/`: objective, acceptance criteria, explicit non-overlapping `owned_paths` for writers, validation commands, Jev evidence, and `review.author_provider` when reviewing (`claude` for work done in this session).
2. `python RUNNER status --activity ACTIVITY --config ROUTING` reads local quota and priority only.
3. `python RUNNER dispatch --activity ACTIVITY --workspace WORKSPACE --task-file TASK --config ROUTING` runs one ordered chain. Launch it with the Bash tool's `run_in_background: true` because a route can run for up to an hour, then rely on the completion notification. Use `--provider NAME` or `--profile NAME` only for an explicit choice (a pinned profile must be in that activity's chain), and `--dry-run` to inspect the command without inference.
4. Read the result JSON. Exit 0 with a completed status means a provider produced a validated terminal result, not that the work is accepted: check the handoff, rerun or reuse checks as policy requires, and integrate. Exit 0 with `status: dry_run` is not a result. Exit 20 means no route completed (`all_routes_unavailable`); read `attempts` for the reason (quota, authentication, setup, disabled route or unresolved state). The task is unfinished.

The dispatcher shares one quota cache per account and advances only after a known terminal failure and a resolved writer claim. A timeout, cancellation or malformed terminal keeps the claim: inspect `pending_path`, confirm the old session stopped, and only then resolve it. Preserve partial edits; never clear claims, quota files or logs to force progress. Disjoint ownership may run concurrently. Ordinary workers never delegate further; `orchestration` needs `allow_delegation: true` in the contract.
