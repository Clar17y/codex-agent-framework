# Codex agent framework

Install the same personal agent roles, Gemini implementation route, and Claude review route on Windows, macOS, or Linux. The primary Codex model stays your choice.

## Install on your Mac

You need **Python 3.11+**, Git, and Codex signed into your account. If Python is missing or older, install a current version from [python.org](https://www.python.org/downloads/macos/) and reopen Terminal.

Clone this private repository using your normal GitHub authentication:

```bash
git clone https://github.com/Clar17y/codex-agent-framework.git
cd codex-agent-framework
python3 --version
python3 install.py --dry-run
python3 install.py
```

Alternatively, download the ZIP from [Releases](https://github.com/Clar17y/codex-agent-framework/releases), extract it, open Terminal in the extracted directory, and run the same Python commands. No `pip install` is needed.

Start a **new Codex task** after installing. Ask it to list the available custom agents and confirm that `ask-gemini`, `ask-claude`, and `simplify` are available. The framework installs twelve personal roles. Custom role discovery depends on the Codex host; see [OpenAI's custom-agent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents). A host without custom-role support must explicitly pass the role's model, effort, and instructions when spawning.

### Connect the external providers

Install and sign into the **Antigravity CLI (`agy`)** and **Claude Code CLI (`claude`)** on the Mac, following their official instructions:

- [Antigravity installation and authentication](https://www.antigravity.google/docs/cli/install/)
- [Claude Code quickstart](https://code.claude.com/docs/en/quickstart)

The framework installer does not install provider software or transfer sign-in credentials. It can install the native roles even when a provider CLI is absent, and reports the missing CLI. Once the providers are installed and visible on `PATH`, run:

```bash
command -v agy
command -v claude
python3 install.py --refresh-routing
```

If a CLI is outside `PATH`, supply its full path:

```bash
python3 install.py --gemini "$HOME/.local/bin/agy" --claude "$HOME/.local/bin/claude"
```

The existing routing is preserved: Gemini `gemini-3.8-flash-medium`, Claude `claude-opus-5` with medium review effort by default, and the configured native fallback roles. **Installation does not establish that these models are available to your account.** The Mac handoff below includes live checks.

### Windows

Use the same source package in PowerShell, with a Python 3.11+ interpreter:

```powershell
python --version
python install.py --dry-run
python install.py
```

Use an explicit Python executable path if `python` does not identify the right interpreter. There is no dependency on a particular Codex runtime cache path or WinGet package directory.

## What gets installed

The default root is `~/.codex`. The installer honors `CODEX_HOME`; `--codex-home /path/to/codex` overrides it.

| Destination inside the Codex root | Contents |
| --- | --- |
| `agent-framework/` | Provider runner, templates, documentation, and locally generated `routing.json` |
| `agents/` | Twelve custom agent TOML definitions |
| `skills/ask-gemini/`, `skills/ask-claude/`, `skills/simplify/` | Provider delegation and explicit simplification workflows with local paths |
| `AGENTS.md` | One managed routing-policy block; surrounding instructions are preserved |

Existing replaced files are backed up under `agent-framework/backups/`. `agent-framework/install-manifest.json` records file hashes and backup paths. Unrelated configuration, plugins, roles, skills, credentials, and runtime state are preserved. The installer refuses linked source/target paths and ambiguous policy markers instead of guessing. Run it from an extracted or cloned source package outside the destination Codex directory.

If a Mac source folder has a symlinked ancestor (including some synced-folder layouts), use its physical path: from the extracted folder run `python3 install.py --source "$(pwd -P)"`. Choose a physical destination path too; links inside the Codex installation remain prohibited. An existing `.agent-framework-install.lock` blocks both installation and dry runs until the previous installer is confirmed stopped and the stale lock is removed.

**Stop active framework jobs before upgrading.** The installer lock coordinates installer processes; it does not coordinate provider jobs. If installation is interrupted, inspect the reported backup directory and any installer lock before retrying. Installation is not a filesystem-wide transaction; a reported partial failure may require restoring backed-up files.

Provider invocations retain the original explicitly authorized CLI bypass flags. Gemini can edit within the assigned task scope; Claude is restricted to read/search tools, with shell, edits, and MCP excluded. These CLI settings do not bypass the parent Codex permissions. Review [the complete routing and execution policy](docs/FRAMEWORK.md) before using the framework on another person's machine.

## Updates and recovery

From the source clone:

```bash
git pull --ff-only
python3 install.py --dry-run
python3 install.py
```

On Windows, substitute `python`. ZIP users should download and extract a new release, then run its installer. Updates preserve installed routing settings; `--refresh-routing` resets routing from the packaged defaults and rediscovers executables. `--gemini` or `--claude` updates only the selected executable.

To restore an overwritten file, use its `backup` path in the installation manifest. Files that were newly created have no prior backup. Do not restore or delete entire Codex directories: they also contain unrelated settings and sessions. There is no automatic uninstaller.

Each install retains a separate backup snapshot (empty on a fresh install). Backups are not pruned automatically. Files removed from a future source release also remain installed; any future role removals or renames need explicit migration instructions. This version does not remove or rename existing roles.

Keep the source clone separate from the installed copy. The copied installer is retained for inspection; upgrades must run from a complete source package, which also includes the role and skill sources.

## Validation and Mac handoff

Run all offline tests from the source directory:

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Tests use temporary installation roots and fake provider processes under `.llm-output/`. They never use real provider credentials or modify your live Codex installation. GitHub Actions runs the suite on Windows, macOS, and Linux. These checks do not prove authentication, pinned-model availability, or role discovery in the Mac's Codex app.

On the Mac, open this repository in a new Codex task after installation and use this handoff:

> Read README.md and docs/FRAMEWORK.md. Verify my installed framework without modifying unrelated Codex settings. Confirm all twelve roles, both provider skills, and the simplify skill are available. Inspect the locally generated executable paths. Run offline tests. Then run one bounded implementation smoke task and one read-only review smoke task in a scratch workspace with harmless fixture files and explicit ownership. Preserve model pins, CLI restrictions, and pending-run protections; report missing access accurately and verify the native fallback route if needed. Record actual Mac results in issue #1. Never transfer credentials, clear unresolved pending runs, or claim simulated tests establish live provider availability.

The [Mac port issue](https://github.com/Clar17y/codex-agent-framework/issues/1) tracks machine-specific validation. Account quota is shared by the provider account, but quota caches and ownership state are local to each machine. Use separate working copies; the framework does not coordinate writers across computers or network-mounted workspaces.

## Source package

This repository contains the reviewed framework source, twelve role definitions, three skills, tests, and optional task/report templates. It excludes Windows runtime state, pending jobs, logs, credentials, personal `config.toml`, backups, and installation manifests. `{{CODEX_ROOT}}` in policy and skill source files is an installer placeholder, replaced with the selected absolute path.

The provider runner is imported without behavior changes. Historical benchmark notes are retained in [docs/BENCHMARKS.md](docs/BENCHMARKS.md) as supplied historical evidence, not new performance measurements. No third-party provider executables are bundled. The repository remains private; no open-source license has been selected.

The roles include their own simplification self-check; they do not require invoking the full simplify skill. Use the installed `simplify` skill explicitly for its coordinated review-and-repair workflow. Its provider dependencies are included in this package.
