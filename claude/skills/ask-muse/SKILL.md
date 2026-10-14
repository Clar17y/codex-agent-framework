---
name: ask-muse
description: Run a bounded coding activity on Muse (Wonder or Wonder Full) through the shared dispatcher; a strong route for complex and long-horizon implementation, test design and review.
---

Use the installed framework at `{{CLAUDE_ROOT}}/agent-framework`.

1. Write or reuse a task contract in the workspace's `.llm-output/` (template: `{{CLAUDE_ROOT}}/agent-framework/task-template.json`).
2. Check routes: `python "{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py" status --activity ACTIVITY --config "{{CLAUDE_ROOT}}/agent-framework/routing.json"`
3. Dispatch with Muse pinned, as a background Bash command, and wait for its completion notification:
   `python "{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py" dispatch --activity ACTIVITY --provider muse --workspace "WORKSPACE" --task-file "WORKSPACE/.llm-output/TASK.json" --config "{{CLAUDE_ROOT}}/agent-framework/routing.json"`
   Use `--profile muse-wonder`, `muse-full` or `muse-full-high` to pin a model and effort. A pinned profile must appear in that activity's chain in `routing.json`; otherwise the dispatcher rejects it, so check `status --activity` first or edit the chain. By default `muse-full-high` is only in `security_review` and `deep_review`.

All Muse profiles share one account quota cache and pending writer inventory. Confirm the browser subscription sign-in separately; stored Muse API keys are stripped from children and could otherwise override the account. Static activities disable shell, workspace writes and web tools at the CLI. Cross-provider retries happen only after a known terminal failure and resolved ownership.
