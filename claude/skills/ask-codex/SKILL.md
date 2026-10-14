---
name: ask-codex
description: Run a bounded coding activity on OpenAI Codex (GPT Sol, Luna or Astra) through the shared dispatcher, for example an independent review of Claude-authored changes or a pinned Codex implementation.
---

Use the installed framework at `{{CLAUDE_ROOT}}/agent-framework`.

1. Write or reuse a task contract in the workspace's `.llm-output/` (template: `{{CLAUDE_ROOT}}/agent-framework/task-template.json`). Writing activities need explicit non-overlapping `owned_paths`. Reviews need the saved candidate, `review.diff_path` and `jev.review` evidence; set `review.author_provider` to the author (`claude` for work done in this session).
2. Check the local account and route state:
   `python "{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py" status --activity ACTIVITY --config "{{CLAUDE_ROOT}}/agent-framework/routing.json"`
3. Dispatch with Codex pinned, as a background Bash command, and wait for its completion notification:
   `python "{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py" dispatch --activity ACTIVITY --provider openai --workspace "WORKSPACE" --task-file "WORKSPACE/.llm-output/TASK.json" --config "{{CLAUDE_ROOT}}/agent-framework/routing.json"`
   `--provider openai` takes the first eligible OpenAI profile in that activity's chain. Use `--profile openai-luna`, `openai-sol`, `openai-sol-high` or `openai-astra` to pin a model instead (the two flags cannot be combined). High or deep review requires `--review-reason`. A pinned profile must appear in that activity's chain in `routing.json`; otherwise the dispatcher rejects it, so check `status --activity` first or edit the chain. By default `openai-astra` is only in `deep_review`.

The Codex subscription allowance is the smallest in this setup, so spend it where a different vendor adds the most: independent and security review of Claude-authored candidates, and complex implementation when the earlier routes are exhausted. Prefer Luna for bounded reads and verification. Astra may not be available on every ChatGPT plan; a model error is reported separately from quota and the dispatcher moves on.

The child runs `codex exec` with ChatGPT login forced, API-key overrides removed, an ephemeral session, and a read-only sandbox for static activities. Sign in once with `codex login` (ChatGPT account, not an API key). A confirmed usage limit blocks every OpenAI profile until reset or the probe interval; never clear quota files or switch state directories to get around it. A timeout or malformed terminal keeps the writer claim: inspect `pending_path` before any overlapping retry.
