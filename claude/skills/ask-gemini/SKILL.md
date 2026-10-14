---
name: ask-gemini
description: Run a bounded coding activity on Gemini (Antigravity agy, Flash medium or high) through the shared dispatcher; the default first route for routine implementation, testing, verification and documentation.
---

Use the installed framework at `{{CLAUDE_ROOT}}/agent-framework`.

1. Write or reuse a task contract in the workspace's `.llm-output/` (template: `{{CLAUDE_ROOT}}/agent-framework/task-template.json`) with objective, acceptance criteria, explicit owned paths for writers, validation commands and Jev evidence.
2. Check routes: `python "{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py" status --activity ACTIVITY --config "{{CLAUDE_ROOT}}/agent-framework/routing.json"`
3. Dispatch with Gemini pinned, as a background Bash command, and wait for its completion notification:
   `python "{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py" dispatch --activity ACTIVITY --provider gemini --workspace "WORKSPACE" --task-file "WORKSPACE/.llm-output/TASK.json" --config "{{CLAUDE_ROOT}}/agent-framework/routing.json"`
   Omit `--provider` to let the activity chain choose, which already starts with Gemini for routine work. `--profile gemini-flash-high` pins high effort where that activity's chain contains it (complex, long-horizon, review, planning and test-design chains by default). A pinned profile must appear in that activity's chain in `routing.json`; otherwise the dispatcher rejects it, so check `status --activity` first or edit the chain.

Static activities require the installed `framework-reader` Antigravity agent (`~/.gemini/config/agents/framework-reader.md`), which the dispatcher validates before launch; `--mode plan` alone is not a read-only boundary. Sign in to `agy` with the Google AI subscription account. A confirmed usage limit blocks every Gemini profile until reset or probe. Keep the task contract, ownership, Jev evidence, quota cache and pending state intact; a blocked writer claim must be resolved before any overlapping fallback.
