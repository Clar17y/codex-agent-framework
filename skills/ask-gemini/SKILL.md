---
name: ask-gemini
description: Delegate a bounded activity to Gemini through the shared dispatcher or use the legacy pinned Gemini implementation command.
---

Use the shared framework at `{{CODEX_ROOT}}/agent-framework`. For a normal task, run `provider_runner.py status --activity ACTIVITY --config ROUTING`, then `provider_runner.py dispatch --activity ACTIVITY --workspace WORKSPACE --task-file TASK --config ROUTING`. To explicitly choose Gemini, add `--provider gemini`; this selects the first eligible Gemini profile in that activity. The default implementation chain starts with Gemini Flash medium. Keep the task contract, ownership, Jev evidence, quota cache and pending state intact. Gemini static activities require the installed validated `framework-reader` custom agent; plan mode alone does not restrict writes or commands.

The legacy `implement --provider gemini` entry point remains pinned to Flash medium and retains its historical native Luna fallback semantics. Use it only when explicitly preserving an old workflow. A blocked writer claim must be resolved before any overlapping fallback.
