# Claude Code host

The framework can run with Claude Code as the primary orchestrator. The dispatcher, routing file, quota state and task contracts are the same as the Codex host; only the host integration differs. The Claude install is separate from a Codex install and keeps its own `routing.json` and `state/`.

## Install

```powershell
python install.py --host claude --dry-run   # list destinations, no writes
python install.py --host claude
```

The default root is `~/.claude` (`CLAUDE_CONFIG_DIR` or `--claude-home` overrides it). The installer writes:

| Destination | Content |
| --- | --- |
| `~/.claude/CLAUDE.md` | Framework policy inside `<!-- BEGIN/END CLAUDE MULTI-PROVIDER FRAMEWORK -->` markers. Text outside the markers is preserved. |
| `~/.claude/agents/*.md` | Twelve native roles on Haiku, Sonnet or Opus, rendered from `agents/*.toml` (see below). |
| `~/.claude/skills/` | `route-activity`, `ask-codex`, `ask-gemini`, `ask-muse`, `framework-simplify`, `jev-search`, `jev-review`. |
| `~/.claude/agent-framework/` | Runner, Jev helpers, docs, task template, `routing.json` from `routing.example.json` with `claude/routing.overlay.json` applied, and `state/`. |
| `~/.gemini/config/agents/framework-reader.md` | Antigravity read-only helper, shared with a Codex install; either host's manifest (default roots or `CODEX_HOME`/`CLAUDE_CONFIG_DIR`) counts as ownership when upgrading it. |

Existing agents or skills with the same names are replaced only when the previous manifest shows this installer wrote them; any other collision stops installation before writing. Backups go to `agent-framework/backups/`. Start a new Claude Code session afterwards so the roles and skills load.

Install and sign in to the provider CLIs first: `codex` (`codex login` with the ChatGPT account), `agy` (Google AI subscription), `muse` (browser sign-in) and `claude`. The installer records executable paths; it does not log in or prove entitlement.

## Where it runs

Dispatch needs the signed-in CLIs, so it works from Claude Code on the machine where they are installed (terminal, IDE extension, or the desktop app's Code tab). A claude.ai chat or Project runs in a cloud sandbox without those CLIs and cannot dispatch.

## Default routing

`claude/routing.overlay.json` replaces only the `activities` key of the shared `routing.example.json`, so profiles, providers and workflow defaults stay in one place. It assumes Claude Max as the orchestrator budget, Codex on the smaller ChatGPT plan, Google AI Pro and Muse Power:

- Routine work (exploration, implementation, testing, verification, documentation, docs research, refactor audit) starts on Gemini Flash, then Muse Wonder, with Claude and Codex Luna last.
- Complex and long-horizon implementation and test design start on Muse Wonder Full, with Claude Opus and Codex Sol high behind it.
- Independent and security review start on Codex Sol, so Claude-authored work is reviewed by another vendor by default; set `review.author_provider` to `claude` for work done in the Claude session and the dispatcher moves Claude profiles to the back.
- Planning and orchestration start on Claude Opus; in practice the Claude session plans directly.
- Deep review starts on Claude Opus high, then Astra, which may not be available on every ChatGPT plan.

These are quota-driven defaults, not measurements. Edit the chains in the installed `routing.json`; reinstalling preserves your edits.

## Derived roles and skills

The Claude roles and the `framework-simplify` skill are not kept as separate copies. At install time `claude/derived.toml` renders each `agents/<role>.toml` into Claude Code frontmatter (tools, model alias, optional description) followed by its `developer_instructions`, and renders `skills/simplify/SKILL.md` as `framework-simplify`. Host-specific wording is a list of exact whole-line replacements. Edit a role in its TOML once; if an edit changes a line that a replacement targets, installation stops before writing and names the file, so update the `old` line in `claude/derived.toml` to match.

## Running a dispatch

Long dispatches exceed the Bash tool's foreground limit. Start them with `run_in_background: true` and wait for the completion notification:

```bash
python "$HOME/.claude/agent-framework/scripts/provider_runner.py" dispatch \
  --activity implementation --workspace "C:/dev/repo" \
  --task-file "C:/dev/repo/.llm-output/task.json" \
  --config "$HOME/.claude/agent-framework/routing.json"
```

## Environment handling

Claude children start as fresh subscription sessions: the runner removes `CLAUDECODE`, host `CLAUDE_CODE_*` session variables (keeping only `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_GIT_BASH_PATH`) and every `ANTHROPIC_*` override. Children of other vendors lose the same variables and `CLAUDE_CODE_OAUTH_TOKEN` as well. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY` and the OAuth token value are redacted from run artifacts. Claude children routed through a corporate gateway, Bedrock or Vertex are therefore unsupported. Every child also receives `FRAMEWORK_DISPATCHED_WORKER=1` (`orchestration` for orchestration runs); the installed CLAUDE.md block tells a Claude child carrying it to follow only its task contract instead of acting as the orchestrator. Confirm the variable names your Claude Code version exports with `env | grep -i claude` from its Bash tool; extend the filter if a new session marker appears.

## Validation limits

Offline fake-CLI tests cover routing, ownership and environment filtering. A first signed-in `--dry-run` and one small bounded dispatch per provider are still needed to confirm authentication, entitlement and tool restrictions on this machine.
