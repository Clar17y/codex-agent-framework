---
name: ask-muse
description: Run a bounded Muse activity through the shared dispatcher or use the legacy Muse implementation profile.
---

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py status --activity ACTIVITY --config ROUTING` and `provider_runner.py dispatch --activity ACTIVITY --provider muse --workspace WORKSPACE --task-file TASK --config ROUTING` for the selected activity. The editable chain chooses regular Wonder or Wonder Full. All Muse profiles share one account quota cache and pending writer inventory. Confirm browser subscription sign-in separately; stored API credentials can override account choice, and the framework does not read them. A static activity disables shell and workspace writes at the CLI.

The legacy `implement --provider muse --muse-profile routine|complex|escalation` command remains available with its historical native Luna fallback semantics. The canonical dispatcher handles cross-provider retries only after a known terminal failure and resolved ownership.
