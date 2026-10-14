# Multi-provider activity routing

This package routes bounded coding activities across OpenAI Codex CLI, Claude Code, Google Antigravity (`agy`) and Muse Code. The primary host model remains the user's selection. An explicitly chosen CLI agent can orchestrate by calling the same dispatcher for child tasks; the framework does not run a separate autonomous pipeline.

Install with `python install.py` (Python 3.11+). The additive v12 installer preserves saved provider executables, timing, custom model/profile/chain choices, quota state and credentials. It registers a narrowly named Antigravity reader agent and rejects a conflicting definition. Use `--dry-run` to inspect planned changes; actual signed-in provider access must be checked separately.

The canonical commands are:

```powershell
$framework = "$HOME/.codex/agent-framework"
python "$framework/scripts/provider_runner.py" status --activity implementation --config "$framework/routing.json"
python "$framework/scripts/provider_runner.py" dispatch --activity implementation --workspace "C:/path/to/repo" --task-file "C:/path/to/repo/.llm-output/task.json" --config "$framework/routing.json"
```

Choose from `orchestration`, `planning`, `exploration`, `implementation`, `implementation_complex`, `implementation_long_horizon`, `test_design`, `testing`, `verification`, `independent_review`, `security_review`, `refactor_audit`, `documentation`, `docs_research`, and `deep_review`. `--profile NAME` pins one eligible profile, while `--provider NAME` selects the first eligible profile on that provider. `--dry-run` shows the selected command without inference. Activity chains and profiles live in `routing.json`.

The dispatcher shares one quota cache per account across all models and activities. It moves to the next route after a known terminal failure and resolved writer claim. Unknown completion, cancellation and timeout retain the pending claim, blocking overlapping work. An all-routes-unavailable result is not completion. File ownership and Jev evidence remain part of each task contract. See [FRAMEWORK.md](docs/FRAMEWORK.md) for details.

Static analysis activities use restricted read/search tools. Writing activities require explicit owned paths. A `verification` worker can execute checks and write generated/scratch artifacts within its contract. Independent review starts fresh against a saved candidate and Jev diff binding, and prefers a provider other than the author when one is eligible. High/deep review requires a concrete reason.

The legacy `implement`, `review`, `status --provider` and `quota-set` commands remain for existing scripts. Their pinned-provider fallback metadata does not define the new activity chains. Retired DeepSeek code and state remain for compatibility and audit; the v12 chain does not route to it.

This is local process, cache and claim coordination on one machine. CLI permissions and secret filtering add defense in depth; they are not a cross-process security boundary. Offline fake-CLI tests establish parser and routing behavior, while live authentication, account entitlement and provider-enforced tool capabilities require bounded signed-in validation.

## Claude Code host

`python install.py --host claude` installs the same dispatcher for a Claude Code primary: policy in `~/.claude/CLAUDE.md`, native roles in `~/.claude/agents/`, skills (including `ask-codex`) in `~/.claude/skills/`, and Claude-specific default chains from [claude/routing.overlay.json](claude/routing.overlay.json). It is separate from a Codex install and keeps its own state. See [CLAUDE_HOST.md](docs/CLAUDE_HOST.md).

## Setup and operation

Install the external CLIs and sign into the intended subscription accounts before expecting any route to run. Codex uses its ChatGPT login path; a stored API key or alternate provider configuration must not be assumed to use the subscription. Muse Power needs its interactive browser sign-in and an entitled CLI model ID; regular Wonder and Wonder Full access should be checked separately. Claude and Gemini likewise need installed, signed-in CLIs. The installer discovers executable paths but does not log in, purchase a plan, redeem credits or establish model entitlement. Jev search/review evidence is prepared by the primary/native agent and referenced in task JSON; external children do not inherit its API key. See [JEV.md](docs/JEV.md).

Copy [task-template.json](task-template.json) into the active repository's `.llm-output/` and fill its objective, explicit owned paths, acceptance criteria, checks, instructions and Jev evidence. `status --activity` is a local cache read; it does not create a provider run. `dispatch --activity` executes one ordered chain. Use `--profile` or `--provider` for an explicit choice, `--dry-run` to inspect the command, and `--state-dir` only when every cooperating task deliberately shares that alternate state. The same installed state directory must govern all workspaces using an account.

Results and redacted artifacts are saved under the task workspace's `.llm-output/agent-framework/`. A validated completion still needs the specified tests and primary acceptance. Exit 20 means confirmed or cached quota; `all_routes_unavailable` names attempted routes. For timeout or malformed terminal output, inspect `pending_path`, confirm the old writer and provider session have stopped, and only then resolve the pending record. Do not delete logs, clear quota files or launch an overlapping fallback to force progress. Disjoint file ownership may continue. The [operational guide](docs/FRAMEWORK.md) covers state recovery, orchestration child markers, review binding, and legacy command behavior.

After source changes, run `python -m unittest discover -s scripts -p 'test_*.py' -q`. These tests use local fake processes. Live signed-in probes and repository CI are separate acceptance gates.
