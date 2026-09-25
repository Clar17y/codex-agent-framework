# Multi-provider agent framework v5

Relative to the configured Codex root (default `~/.codex`), the framework lives in `agent-framework/`, native role files in `agents/`, provider skills in `skills/`, and the routing entrypoint in the managed block of your `AGENTS.md`. Use the root selected during installation when `CODEX_HOME` or `--codex-home` overrides the default.

## Routing

Native Sol and Luna roles below use `gpt-6-sol` and `gpt-6-luna`. The [model routing decision](MODEL_ROUTING.md) records the sources and rationale for this allocation; these choices are not new framework benchmark results.

| Work | Primary | Fallback / escalation |
| --- | --- | --- |
| Routine implementation | agy `gemini-3.8-flash-medium` | native `implementer`, `gpt-6-luna`, medium, on Gemini quota exhaustion or non-quota failure |
| Independent review | Claude `claude-opus-5-5` (routine: effort medium; complex: high with reason) | native `reviewer`, Astra low, if Claude unavailable |
| Complex bounded implementation | GPT-6 Sol medium | Sol planning/gate as risk requires |
| Planning and adversarial tests | GPT-6 Sol medium | Sol high for high-risk correctness |
| Explorer, docs, refactor audit, verifier | Luna medium | task-specific escalation |
| Security and high-risk correctness gate | Sol high | separate from ordinary review |
| Exceptional exhaustive gate | GPT-6 Sol max | explicit exceptional use only |

Claude `claude-opus-5-5` review explicitly defaults to `--effort medium` for routine reviews. High effort (`--review-effort high`) is permitted only for justified complex reviews (coupled contracts, state, concurrency, security, or unresolved findings) and strictly requires a non-empty `--review-reason`. Do not raise review effort just due to diff size; large diffs without complex architectural coupling remain medium effort. The Claude invocation supports only medium or high effort and remains pinned to Opus 5.5; the separate native fallback remains available. Review remains strictly Claude-only. Consider a scoped Sol-high correctness gate when a distinct high-risk question warrants a separate check. No Sonnet or Fable route is permitted. The primary session remains the user's choice. The original v3 benchmark summaries are preserved in `docs/BENCHMARKS.md`; they do not establish Gemini or Opus performance.

## Before implementation

Before assigning files or editing, treat the reported example as evidence, not necessarily the full scope. For a bug, trace the cause to its owning component or layer and look for related affected uses; for a feature, identify the intended flow, integration points, and existing reusable components. Choose the smallest complete change, respecting explicit user exclusions and avoiding speculative expansion. Keep this proportional: a quick search and inspection may suffice; use the existing explorer for a bounded investigation when useful. Briefly state the cause or intended behavior, affected scope, chosen location, and any deliberate exclusions in conversation or the task handoff. No separate scope report, persona, or approval stage is needed; surface material scope decisions to the user when necessary.

Substantial delegated workers own the complete bounded outcome: local investigation, implementation, focused validation, routine repairs, command waiting and interpretation, and self-review. Implementers confirm the proposed scope and resolve ordinary implementation and design decisions autonomously within the contract. They escalate material product or architectural decisions, ownership conflicts, missing authorization or unavailable prerequisites, and repeated failures requiring reassessment. The primary remains responsible for scope, architecture, decomposition, ownership boundaries, material decisions, integration, risk assessment, and final acceptance; it does not duplicate the worker's routine execution.

## Implementation handoff

Before handing over implementation, inspect your changes for reuse, clarity, and unnecessary work. Apply justified simplifications within your ownership, preserve observable behavior and others' edits, and rerun affected checks if needed. Perform this pass yourself; separate review or verification is chosen by the primary according to risk. Return a concise handoff including completion or blocker status, changed paths within assigned ownership, key design decisions, candidate identity (e.g. HEAD plus scoped diff/status), exact checks and results, residual concerns, and useful log locations, without routine status chatter or raw command dumps.

## Invocation

When delegating through the provider adapter, copy `task-template.json` to the active workspace's `.llm-output/` and fill it with real criteria, owned paths, scoped instructions and checks. Always use the repository's provisioned worktree when required. For review, include candidate HEAD, base revision and the path to a saved integrated diff in the objective; Claude has read/search tools only, so it cannot generate a diff with shell commands.

```bash
framework_root="${CODEX_HOME:-$HOME/.codex}/agent-framework"
python3 "$framework_root/scripts/provider_runner.py" implement \
  --workspace "$PWD" --task-file "$PWD/.llm-output/task.json" \
  --config "$framework_root/routing.json"
```

```powershell
$frameworkCodex = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$frameworkRoot = Join-Path $frameworkCodex 'agent-framework'
python "$frameworkRoot/scripts/provider_runner.py" review --workspace "$PWD" --task-file "$PWD/.llm-output/task.json" --config "$frameworkRoot/routing.json"
```

Replace `implement` with `review` for Opus. Routine review explicitly passes `--effort medium` to Claude by default. For complex reviews involving coupled contracts, state machines, concurrency, security, or unresolved findings, pass `--review-effort high --review-reason "<reason>"`. A non-empty reason is strictly required for high effort; do not raise review effort solely due to diff size. Review effort and reason flags are rejected for the `implement` role. Selected review effort and reason are recorded in result and dry-run metadata. `--dry-run` inspects planned invocation without contacting a provider. Run via the normal shell tool; if the Codex sandbox cannot access the installed CLI or global quota state, request the normal command escalation. The adapter uses CLI bypass flags for Gemini and Claude as explicitly requested by the user. Subprocess credential filtering and environment sanitization are retained as defense in depth, not a hard same-user process-containment boundary. These settings do not bypass Codex sandbox or automatic approval review. A CLI or adapter launched through shell is not a native Codex subagent; its logs and structured result are the handoff to the parent.

## Results and quota handling

- Exit 0: provider completed; read its structured result and evidence. Reuse credible matching evidence; worker claims alone are not test evidence. Rerun checks only when relevant inputs changed, evidence is missing or unreliable, explicit repository requirements mandate it, or a specific concern warrants independent verification.
- Exit 20 / `fallback_required`: inspect the result's `fallback` metadata:
  - When Gemini quota is exhausted, the adapter routes implementation directly to native `implementer`, `gpt-6-luna`, medium.
  - When Claude review quota is exhausted, it falls back to native Astra low `reviewer`.
  Carry over the same contract, captured partial changes and prior checks. An active cached result satisfies the provider-first routing requirement; do not launch an exhausted provider again for another operation.
- Other nonzero exit, including `blocked_pending_run`: report the actual provider/setup/timeout blocker and use the pre-authorized native fallback without asking again, once file ownership is safe. Non-quota Gemini failures route directly to Luna medium. It is not proof of quota exhaustion; do not modify quota state to force fallback. A previous writer must be stopped before handing over overlapping files; an unresolved run in another workspace does not block a fresh, non-overlapping Luna task. Keep pending-state protection intact and respect Codex permissions and safety review.

Gemini implementation tasks may run concurrently in separate worktrees and within the same workspace when their task contracts declare disjoint `owned_paths`. The adapter holds a short coordination lock (`gemini.lock`) while checking and recording ownership; it does not hold a workspace lock throughout provider execution. Ownership entries are literal file or directory paths, not glob patterns. Exact path matches and ancestor/descendant paths conflict, so owning a directory covers everything below it. Paths are resolved and Windows case is normalized. An empty `owned_paths` list conservatively claims the entire workspace for implementation. Active or uncertain runs retain their ownership claims until resolved. These declarations coordinate cooperating adapter runs; they do not enforce an OS-level file sandbox, and agents must keep edits within their assigned paths. Claude reviews have no ownership lock or pending-run gate and may run concurrently, including in the same workspace; callers must choose a stable candidate for review. Providers briefly lock quota updates, without serializing provider execution. Do not run old adapter copies alongside the updated adapter: older copies do not honor all ownership and quota protections.

Confirmed terminal quota errors are cached globally in `state/gemini-quota.json` and `state/claude-quota.json` (legacy `state/deepseek-balance.json` snapshots are preserved if present), using short update locks and atomic writes. Concurrent responses must not shorten an active block; an older in-flight success does not clear a newer exhaustion record. Already-running requests may finish after another request reports exhaustion. A known provider reset is used when unambiguous; otherwise `quota_probe_seconds` (default 3600) is a probe cooldown, not a known reset. Subsequent operations return the provider's native fallback during that period. Successful responses merely mentioning quota, authentication/setup failures, and transient 429/throttling must not activate quota fallback. Claude distinguishes plan usage limits from temporary server throttling in its [error reference](https://code.claude.com/docs/en/errors#youve-hit-your-session-limit).

Per-run logs, task prompt, results and git evidence live in the active workspace's `.llm-output/agent-framework/`. Do not delete scratch logs. Partial files are preserved, and the parent must ensure the previous writer has stopped before handing off. Timeouts attempt CLI process-tree termination, but direct-PID observation cannot verify escaped descendants or provider-backend cessation. Per-run pending records under `state/workspaces/<workspace-hash>/gemini-pending/<run-id>.json` preserve each task's ownership after uncertain termination. Results include `pending_path` for the relevant record. Existing workspace-wide pending records and the legacy global `state/gemini-pending.json` are preserved and checked. A legacy record's explicit workspace or standard log path identifies its workspace; without recorded file ownership it conservatively protects that entire workspace. A legacy record whose workspace cannot be identified still blocks all launches until investigated; it is never silently cleared. Only after confirming that session has stopped may the operator change its `status` to `resolved`; preserve the evidence and do not clear it merely to retry. Scope ownership is a task contract; it is not an OS-level per-file write sandbox. Gemini and Claude use user-authorized CLI bypass mode; task contracts and provisioned worktrees remain essential, and Codex automatic approval review still applies.

## Provider availability and manual entries

Before each external-provider operation, check the installed state without calling external providers:

```bash
framework_root="${CODEX_HOME:-$HOME/.codex}/agent-framework"
python3 "$framework_root/scripts/provider_runner.py" status --provider gemini --config "$framework_root/routing.json"
python3 "$framework_root/scripts/provider_runner.py" status --provider claude --config "$framework_root/routing.json"
```

`status` needs no workspace or task file, creates no run logs, and does not modify state. Gemini and Claude status checks remain strictly local-only and never contact providers. It reports the provider/model, quota file, cached evidence if present, and the appropriate fallback. For a selected provider, `fallback_required` exits 20, `available_to_try` exits 0, and `state_error` exits 1. Route active blocks and state-read failures to the corresponding native fallback, disclosing the actual reason. Availability means only that the last-known quota record permits an attempt; it does not establish current usage, authentication, model access, or safe file ownership. Normal `implement` and `review` calls enforce the same cache before launching the CLI. Gemini pending-run checks still apply, and any fallback writer must respect unresolved ownership even when the quota-only status check is blocked.

Use the installed `routing.json` consistently: by default its sibling `state/` directory is shared across workspaces. `--state-dir` deliberately overrides that location, for example in offline tests; it must not be used to evade an exhausted account's record. State is local to this machine and is preserved during installation updates. The original Gemini `retry_at` cache format remains readable.

New exhaustion records include `observed_at`, `retry_at` (Unix seconds), `retry_at_iso`, nullable `reset_at`, `reason`, `provenance`, and the source error/log evidence. Timestamps are normalized to UTC. `provider_reset` means a known reset; `probe_cooldown` means the actual reset is unknown. Ambiguous dates or timezones use the cooldown. Human reset messages with IANA zones such as `Europe/London` require timezone data available to Python's `zoneinfo`; if absent, the adapter uses the cooldown. An explicit ISO timestamp with a UTC offset does not need that database. Expired records remain as evidence, but allow the next operation to try the provider again. Nothing wakes up to poll usage in the background.

If the user reports exhaustion or supplies a reset from their usage screen, record it locally:

```bash
python3 "$framework_root/scripts/provider_runner.py" quota-set --provider claude \
  --config "$framework_root/routing.json" --reason "User reported exhausted usage"
```

To supply a known reset, append `--reset-at` with a future timezone-qualified ISO timestamp, such as `2099-01-01T15:00:00+00:00` (replace this example with the actual reset). The same command accepts `--provider gemini`. Manual entries are marked `operator_report`; without a reset, they use the configured cooldown. They preserve any longer active block and never change pending-run records. Do not infer a reset from an authentication error, unavailable CLI, or unresolved writer, and do not hand-edit a cache to manufacture provider eligibility.

## Proportionate workflow

Choose implementation, review, and verification effort proportionate to the change's risk and uncertainty. Small, well-understood fixes, including localized behavior changes, can be completed directly with focused checks and self-review. Use independent review or verification when a separate perspective is likely to add meaningful confidence. Security, concurrency, cancellation, timeouts, migrations, data integrity, broad changes, release-critical behavior, and unresolved failures are reasons to consider deeper scrutiny, not automatic triggers for a fixed sequence of agents. Briefly explain the chosen approach in conversation or the PR; a small task needs no separate process record. Repository CI and release requirements remain in force.

The routing table applies when delegation is useful. The primary may implement a small fix directly. When choosing delegation, check the provider's local availability first; an active cached block satisfies that route check and goes directly to the native fallback. If review is chosen, prefer one integrated review; revisit material findings only when the repair or remaining uncertainty needs another independent look. Repeated unsuccessful repairs are a reason to reassess the approach or ownership, rather than continue a fixed loop.

The primary can run checks directly. Delegate verification when it adds useful independence or substantial parallel work, not merely to obtain a fresh agent. Worker claims alone are not test evidence. Reuse credible matching evidence when relevant inputs, commands, and conditions match; do not automatically duplicate verification. Require reruns only when relevant inputs changed, evidence is missing or unreliable, explicit repository requirements mandate it, or a specific concern warrants it. Record the candidate and enough relevant input, command, configuration, and environment context to judge whether evidence still applies. For small tasks, HEAD, the scoped diff/status, and command results usually suffice. Use scoped hashes or aggregate generated-input hashes when reuse or concurrent changes warrant them; avoid exhaustive repository, dependency, ignored-file, or scratch-file inventories. Keep secrets out of records. Review output is not test evidence.

### Review and repair batches

Follow the mandatory [review and handoff discipline](../GLOBAL_POLICY.md#review-and-handoff-discipline). The unit of repair is an affected behavior or invariant, including its relevant callers and failure paths. Collect and adjudicate available findings before returning one batch to its owner. A second cycle exposing another related defect triggers a component-level reassessment before another patch; it is not a limit on fixing bugs.

The usual shape is one complete implementation/validation handoff, one integrated review when warranted, then targeted repair checks. A follow-up contract carries the original findings, changed candidate, affected invariants and reusable evidence. Whole-change reviews, extra gates and broad reruns need a specific remaining question or explicit requirement. For example, several findings about start, retry and cleanup belong in one lifecycle repair; they should not each start a fresh reviewer/gate/verifier chain.

Workers return immediately when their assigned outcome is complete. Recover a delayed report from existing command results instead of repeating completed work. Local and hosted work may overlap after their prerequisites are satisfied; required CI and release gates still determine final acceptance.

These are instructions installed into the orchestrator policy, skills and roles. The provider adapter does not intercept every native-agent or GitHub review call, so this is not a runtime cap on review count. It preserves the ability to address real defects and mandatory checks.

## Lightweight records

Optional `jev-search` and `jev-review` capabilities use the same Python runtime and the TypeSafe API. Search retains raw candidates internally and returns compact source evidence; review returns advisory diff signals. The primary/native explorer prepares this evidence before provider delegation. These tools do not change routing, ownership, quota state, required checks or acceptance. Remote calls require saved exact-workspace authorization under `capabilities.jev` or an already-authorized invocation with `--allow-remote`. `TYPESAFE_API_KEY` is excluded from execution-provider child environments and registered for retained-artifact redaction. See [Jev usage](JEV.md).

Keep useful command logs and provider-required task/result files in the active workspace's `.llm-output/`. Read saved results rather than rerunning unchanged checks. File writes are not a checklist: quick reads and successful short checks usually need no separate file.

For a small task, conversation and PR notes are enough. If continuity or handoff needs a durable scratch record, prefer one `run.md` containing scope, decisions, material findings, and check results. The templates in `docs/` are optional aids for larger work; omit unused fields and do not automatically create a repo map, decisions file, brief, validation manifest, metrics file, and worker report. Collect metrics only when useful for diagnosing workflow cost or requested by the user.

Workers report completion, blockers, owned paths, decisions, candidate identity, and checks concisely. Assign command, test, or CI waiting and failure diagnosis to one owner; avoid routine status polling, chatter, or duplicate monitoring by the primary. Use one designated writer for shared records. Scratch records are reconstructible and may be swept by repository scratch cleanup; keep credentials out of them. The shared quota state lives outside workspace scratch. This framework provides a local quota cache, with no background service, database, or automatic telemetry.

## Maintenance and activation

Claude Opus 5.5 requires Claude Code 2.1.280 or later. Check `claude --version` and run `claude update` (or its package-manager instruction). Use the installer's `--claude` option for an explicit executable path. The v8 routing migration upgrades only the exact pre-v8 packaged `claude-opus-5` model value. It preserves custom model strings, executable paths, effort, timing and other settings, as well as provider-wide quota records. A custom review model outside the supported pin is preserved but rejected by the runner; update it explicitly if review should use Opus 5.5. The v9 routing migration retires DeepSeek from active routing and defaults, bumping the routing version to 9 and removing legacy `providers.deepseek` entries on upgrade while falling back directly from Gemini to native Luna medium.

The default installation root is `~/.codex`; `CODEX_HOME` or `--codex-home` can select another root. Start a new Codex task after installation to discover the new skills, global policy and role files. This existing task's loaded tool/skill catalog is not dynamically rewritten. If the host supports only generic spawning, explicitly pass the installed role's model, effort and instructions.

The installer backs up only files it owns/replaces, merges one marked policy block, preserves unrelated configuration and records hashes in `install-manifest.json`. `routing.json` pins absolute CLI paths discovered during setup; rerun the source installer with `--refresh-routing` if the executables move. Any preexisting user-level `$CODEX_HOME/deepseek.config.toml` profile or credentials (`DEEPSEEK_API_KEY`) remain untouched on the machine. Custom credential names from retired routing entries are retained in `retired_secret_env_vars` solely for subprocess and log filtering; no credential values or enabled provider entries are stored there. Use the Python 3.11+ interpreter available on this machine. See the root README for non-inference validation commands, installation and updates.

Run `python -m unittest discover -s scripts -p test_provider_runner.py` from this framework directory after adapter changes. Tests use fake CLI processes; live smoke tests separately establish authentication and model availability. Do not claim quota detection is live-proven until a real exhaustion response has been captured and matched. Review/test logs must describe that distinction.

An explicit `--refresh-routing` rebuild also discards `retired_secret_env_vars`, including custom credential names from older routing files. Those custom-named environment variables then fall outside the adapter's filtering; remove unused credentials from the environment before a reset. The canonical `DEEPSEEK_API_KEY` remains filtered. Ordinary v9 upgrades retain the filtering metadata.

## Provider lifecycle diagnostics

The adapter normalizes CLI lifecycle telemetry into one provider-neutral contract across Gemini and Claude (plus retained legacy DeepSeek parsers), while preserving each provider's native schema and terminal classification underneath:

- **Gemini** uses `stream-json` output (`event=init`, `event=step_update`, `event=result`).
- **Claude** invokes `--output-format stream-json --verbose` (`type=system`, `type=assistant`, `type=result`), without partial-message streaming flags.
- **Legacy DeepSeek compatibility** retains `--json` under nested Codex (`turn.started`, `turn.completed`, `item.started`, `item.completed`, `turn.failed`).

Raw child NDJSON/JSONL remains in `stdout.log`, while adapter stdout remains reserved for one final JSON result. The adapter incrementally consumes child streams using a raw-byte incremental reader handling partial lines and split UTF-8 boundaries, emits compact throttled step/tool transitions on stderr (e.g. `{provider} progress: events=...; last=...; completed_steps=...; no response text relayed`), and atomically writes `progress.json` in the run log directory. Completion classification reads only a bounded suffix of complete stream records rather than loading the accumulated transcript, and retained-artifact redaction scans and copies in bounded chunks. Redaction uses atomic replacement where possible, a guarded single-link in-place fallback when inherited Windows handles deny replacement, and records any per-file failure in `artifact_errors`. The run result exposes `progress_path` for all providers. That snapshot records event counts, step state, and normalized token counters, but strictly excludes prompts, model response text, reasoning/thinking, tool parameters, shell commands, paths from tool arguments, file contents, and tool output.

Streaming is CLI lifecycle telemetry: it parses CLI stdout and does not ask providers for extra summaries, add LLM turns, or incur provider-token costs. The adapter rate-limits stderr summaries to a bounded rate, so the parent agent receives a small amount of additional context rather than the raw event stream. Raw `stdout.log` may contain model text, tool inputs, or tool results and must be treated as a sensitive run artifact; `progress.json`, heartbeat data, and compact stderr summaries exclude that content.

The configured heartbeat (defaulting to 60 seconds for active Gemini and Claude routes) remains an independent fallback during quiet periods. Positive intervals below 5 seconds are clamped to 5 seconds to bound filesystem and parent-context churn; zero disables periodic heartbeats. Each heartbeat atomically refreshes `heartbeat.json` with local process state, provider-log activity, and the latest compact stream event, emitting `adapter heartbeat: {provider} ...` on stderr. A live process is liveness evidence, and a recent stream event is stronger activity evidence, but neither proves semantic correctness or eventual backend completion. Keep waiting on the existing adapter command unless it returns a terminal result or reaches its configured timeout; do not start an overlapping fallback merely because event output is temporarily quiet. Native subagents continue to use completion notifications and do not need periodic prose-progress files.

Only validated provider terminal parsers establish completion, quota exhaustion, permission denials, pending ownership resolution, or fallback authorization:

- Gemini requires a valid `event=result` payload (or the narrowly supported legacy single terminal envelope).
- Claude requires a valid `type=result` payload parsed via `parse_claude_final_output`.
- The retained DeepSeek parser requires a validated final status object or a terminal `turn.failed` event.

A missing or malformed terminal envelope fails closed to an `uncertain_exit`, not a completed run: implementer pending records keep overlapping ownership blocked and set `fallback_blocked_by_pending` until an operator confirms resolution. Timeout or interruption cleanup records direct-PID observation and termination-attempt outcome only; it cannot verify escaped descendants or provider-backend cessation.

Gemini uses a provider-specific 3600-second soft timeout by default, followed by a 120-second adapter termination grace. Claude inherits the packaged global 900-second timeout (as do legacy direct DeepSeek runs) unless an operator sets a provider-specific value; if neither provider nor global timing is present, the code-level fallback is 1800 seconds. During v5-to-v6 upgrade, an untouched legacy global 900-second configuration receives missing Gemini lifecycle defaults field by field; any existing provider-specific timing field is preserved as authoritative. During v6-to-v7 upgrade, the exact packaged 1800-second Gemini default moves to 3600 seconds; any other provider-specific timeout is treated as an operator override and preserved. A custom legacy global timeout is preserved without inferred provider overrides.
