# Architecture and Design Decisions

This document details the six core architectural decisions governing the multi-provider orchestration framework. Each decision addresses concrete failure modes encountered when coordinating external AI developer CLI tools—Google Antigravity (`agy`), DeepSeek Flash via OpenAI Codex CLI (`codex`), and Anthropic Claude Code CLI (`claude`)—under an OpenAI Codex development environment.

For operational workflows and runtime configuration, see [FRAMEWORK.md](FRAMEWORK.md). For implementation details, see [../scripts/provider_runner.py](../scripts/provider_runner.py).

---

## 1. Fail-Closed Provider Routing and the Three-Tier Fallback Chain

### Problem
External AI provider CLIs suffer from rate limits, unexpected service outages, payment/quota exhaustion, and transient network errors. In an automated multi-agent coding workflow, unhandled provider failures or ambiguous error states can lead to infinite retry loops, hanging tasks, silent abandonment, or uncontrolled model switching. An orchestrator requires a deterministic routing hierarchy that attempts fast, cost-effective models first and reliably falls back to local or alternative providers without stalling or corrupting workspace state.

### Decision
Implement a deterministic, three-tier fail-closed fallback hierarchy for code implementation tasks:

1. **Primary Tier (Gemini Flash)**: Google Antigravity (`agy`) running `gemini-3.8-flash-medium`.
2. **Secondary Tier (DeepSeek Flash)**: DeepSeek Flash v4.1 under the local `deepseek-flash` alias via nested OpenAI Codex CLI (`codex exec -p deepseek`), activated when Gemini reports terminal quota exhaustion, provided DeepSeek is configured and enabled (`providers.deepseek.enabled=true`).
3. **Final Tier (Native Luna Medium)**: Native OpenAI Codex role `implementer` running `gpt-5.6-luna` (medium effort), activated when DeepSeek monetary balance is depleted, preflight validation fails, or Gemini encounters a non-quota error.

Independent code reviews follow a dedicated review route: Anthropic Claude Code CLI (`claude`) running `claude-opus-5` (default medium effort; high effort requires a documented reason), falling back directly to native Astra low (`reviewer`) upon quota exhaustion. DeepSeek is excluded from the review path.

The routing engine fails closed:
- **Exit 0 (success)**: Provider executed and completed successfully (`available_to_try` is the status subcommand's verdict when no cached block is active, rather than a runner execution exit code).
- **Exit 20 (`fallback_required`)**: Confirmed quota exhaustion, zero monetary balance, or disabled provider; signals the orchestrator to advance immediately to the next tier without launching the exhausted CLI again.
- **Exit 1 (`blocked_pending_run` / `balance_check_failed` / `state_error`)**: Unresolved pending runs on overlapping claimed paths, preflight validation error, network failure, or corrupted state. When an unresolved pending run is detected, execution deliberately blocks (`fallback_authorized` is false and `fallback_blocked_by_pending` is true) until an operator resolves it; for other exit-1 errors, the runner directs the orchestrator to fallback while preserving unreadable diagnostic evidence.

### Why It Matters
Fail-closed routing prevents unbounded retries against depleted or failing APIs. Every terminal provider outcome routes to a defined next step except unresolved pending-run conflicts, which deliberately block until an operator inspects and resolves them. By capturing terminal quota events and mapping them to structured exit codes, the orchestrator transitions to secondary or native models without human intervention or conversational prompt loops, maintaining deterministic agent behavior.

### Implementation Citations
- **Runner**: [`../scripts/provider_runner.py`](../scripts/provider_runner.py) — `execute()` (lines 1967–2453), `quota_fallback()` (lines 848–860), `luna_fallback()` (lines 844–847), `finish()` (lines 2453–2484), `command_for()` (lines 345–405).
- **Policy**: [`FRAMEWORK.md`](FRAMEWORK.md) — Section "Routing" and Section "Results and quota handling".

---

## 2. Monetary Preflight and Quota Caching

### Problem
Pay-as-you-go APIs such as DeepSeek require positive monetary credit balances. Launching a coding agent against an account with an empty balance wastes time, produces noisy failure cascades mid-task, and risks leaving dirty workspace edits. Similarly, invoking rate-limited providers such as Gemini or Claude repeatedly after quota exhaustion wastes network round-trips and adds unnecessary latency to agent execution loops.

### Decision
Enforce a mandatory, live monetary-balance preflight check before every DeepSeek invocation, combined with atomic local quota caching across all providers:

1. **Live Monetary Preflight**: Before invoking `codex exec -p deepseek`, the runner sends a direct HTTPS request to DeepSeek's balance endpoint (`GET /user/balance`) using Python's standard library `urllib.request`. A custom `NoRedirectHandler` rejects HTTP redirects to prevent credential leaking. If the endpoint returns HTTP 402, `is_available=false`, or a non-positive total balance, the runner exits 20 (`fallback_required`) before spawning the model process. If authentication fails, the network is unreachable, or a 429/5xx error occurs, it exits 1 (`balance_check_failed`), falling back to Luna without misclassifying the incident as confirmed quota exhaustion.
2. **Atomic Quota Caching**: Confirmed terminal quota errors are recorded in shared JSON cache files (`gemini-quota.json`, `claude-quota.json`, `deepseek-balance.json`) in the framework `state/` directory. Updates are guarded by cross-process file locks (`provider_lock`) and written via atomic file replacement. Cached entries record exact provider reset timestamps when available, or apply a bounded probe cooldown (`quota_probe_seconds`, default 3600s). Status checks inspect these local caches without network overhead unless `--check-live` is explicitly requested.

### Why It Matters
Monetary preflights ensure that DeepSeek is never invoked when account credits are depleted, eliminating mid-run payment aborts and routing immediately to native Luna. Quota caching prevents hammering rate-limited APIs during cooldown periods, allowing subsequent operations to bypass exhausted providers instantly.

### Implementation Citations
- **Runner**: [`../scripts/provider_runner.py`](../scripts/provider_runner.py) — `query_deepseek_balance()` (lines 1082–1172), `NoRedirectHandler` (lines 1077–1080), `classify_balance_error()` (lines 1063–1075), `validate_balance_info_entry()` (lines 878–896), `record_balance_snapshot()` (lines 1193–1255), `quota_state()` (lines 1290–1303), `quota_record()` (lines 1305–1322), `provider_lock()` (lines 254–291).
- **Policy**: [`FRAMEWORK.md`](FRAMEWORK.md) — Section "Provider availability and manual entries".

---

## 3. Credential Isolation Across Child Subprocesses

### Problem
In a multi-provider orchestrator, child subprocesses are spawned to execute different provider CLIs, run git commands, and collect diff evidence. If environment variables are inherited naively, sensitive credentials such as `DEEPSEEK_API_KEY` could be exposed to third-party provider tools (e.g. Google Antigravity or Claude Code) or leaked through git hooks, external diff programs, or child shell environments executed by code-generating models.

### Decision
Implement strict environment sanitization and subprocess boundary controls:

1. **Subprocess Environment Stripping**: The `child_environment()` function strips `DEEPSEEK_API_KEY` (and any configured environment variable name, e.g. `api_key_env`) from environment dictionaries passed to Gemini and Claude provider subprocesses. `git_evidence()` performs its own value-based secret stripping on Git subprocesses by filtering variables whose values match known credentials. Only the DeepSeek execution path receives the DeepSeek API credential verified by preflight.
2. **Nested Codex Shell Isolation**: When invoking nested Codex CLI for DeepSeek (`codex exec -p deepseek`), the runner supplies `-c shell_environment_policy.ignore_default_excludes=false`, preventing child shell commands executed by the model from inheriting environment secrets. Unattended execution passes `--approve-for-me`, selecting the workspace-write sandbox while avoiding conflicts with explicit `--sandbox` flags.
3. **Git Subprocess Hardening**: Git-evidence collection (`git_evidence()`) passes `--no-ext-diff` and `--no-textconv`, and executes git with `-c core.fsmonitor=false`. This prevents untrusted repository configurations from executing external diff drivers, textconv filters, or repository-configured filesystem monitors.

### Why It Matters
Credential isolation provides defense-in-depth against credential harvesting and inadvertent leakage. Even if a model or child tool is compromised or misbehaves, it cannot inspect credentials intended for other providers or leak secrets through external tool hooks.

### Implementation Citations
- **Runner**: [`../scripts/provider_runner.py`](../scripts/provider_runner.py) — `child_environment()` (lines 935–945), `deepseek_secrets()` (lines 924–933), `command_for()` (lines 345–405), `direct_windows_codex_command()` (lines 321–343), `git_evidence()` (lines 1944–1965).
- **Policy**: [`FRAMEWORK.md`](FRAMEWORK.md) — Section "Invocation" and Section "Provider availability and manual entries".

---

## 4. Artifact Redaction of Retained Run Logs

### Problem
Autonomous agent runs produce extensive operational logs in `.llm-output/agent-framework/`, including stdout streams, stderr traces, event progress records, heartbeat snapshots, and git diffs. If API keys, authentication tokens, or authorization headers appear in command outputs, error traces, or tool arguments, persisting these logs to disk creates a critical credential exposure risk in git repositories or shared development machines.

### Decision
Implement post-execution artifact sanitization across all retained output files:

1. **Memory-Mapped Scanning and Bounded Copy Rewriting**: `sanitize_run_artifacts()` scans retained output files (`prompt.txt`, `stdout.log`, `stderr.log`, `last_message.txt`, `provider.log`, `progress.json`, `heartbeat.json`, `status.txt`, `diff.txt`, `head.txt`) by executing `pattern.search` over a full-file memory map (`mmap`). When matching secrets are detected, it rewrites the file using bounded 64 KB copies (`ARTIFACT_COPY_CHUNK_BYTES = 64 * 1024`). `result.json` is not in this file list because it is sanitized separately in memory at result-write time before being persisted to disk.
2. **Secret Masking**: Replaces all occurrences of known secrets (including `DEEPSEEK_API_KEY` and any discovered API key tokens) with redaction markers (`[REDACTED]`).
3. **Resilient File Replacement**: Sanitized contents are written via atomic file replacement (`os.replace`) where supported, with a guarded single-link in-place rewrite fallback to handle Windows environments where active file handles deny renaming.
4. **Error Accounting**: Any redaction failure is captured and reported in `artifact_errors` in the structured run result rather than silently ignored.

### Why It Matters
Retained logs are essential for post-mortem debugging, verification, and auditability, but they must not become secret storage. Performing deterministic redaction across retained artifacts and sanitizing results in memory at write time ensures that logs can be safely inspected, archived, or shared without exposing credentials.

### Implementation Citations
- **Runner**: [`../scripts/provider_runner.py`](../scripts/provider_runner.py) — `sanitize_run_artifacts()` (lines 947–1052), `_sanitize_all_secrets()` (lines 908–922), `_sanitize_secret()` (lines 898–906), `ARTIFACT_COPY_CHUNK_BYTES` (line 31).
- **Policy**: [`FRAMEWORK.md`](FRAMEWORK.md) — Section "Results and quota handling" and Section "Provider lifecycle diagnostics".

---

## 5. File-Ownership Claims and Pending-Run Records Preventing Overlapping Writers

### Problem
Concurrent autonomous coding agents operating within the same workspace can easily collide. If two agents simultaneously attempt to modify the same files, refactor shared modules, or write to overlapping directory trees, the workspace suffers from race conditions, conflicting edits, partial overwrites, and corrupted git state. Furthermore, if an agent process terminates unexpectedly (e.g. crash, hard timeout, or cancelled task), partially applied changes could be overwritten by a new agent before the failure is investigated.

### Decision
Implement hierarchical path-ownership registration and durable pending-run tracking:

1. **Hierarchical Path Matching**: Before launching an implementation task, the runner checks the declared `owned_paths` in the task contract. Paths are canonicalized (resolving symlinks and folding Windows case via `normalized_owned_paths`). Ownership is checked hierarchically via `claims_overlap()`: exact file matches, parent-directory ownership of child files, and child-file claims within a claimed directory are all detected as conflicts. An empty `owned_paths` list conservatively claims the entire workspace.
2. **Atomic Reservation**: Ownership checks and reservations are synchronized using a short-lived coordination lock (`gemini.lock`) via `provider_lock()`. The lock is released immediately after registration so long-running tasks do not block other disjoint writers.
3. **Durable Pending Runs**: Active runs register pending records under `agent-framework/state/workspaces/<workspace-hash>/gemini-pending/<run-id>.json`. If a task finishes abnormally, hits a timeout, or exits with an uncertain status, the pending record remains active on disk, marking the owned paths as blocked (`blocked_pending_run`). New tasks attempting to modify those paths are rejected until an operator or recovery workflow explicitly inspects and marks the pending run as resolved.
4. **Non-Interfering Reviews**: Read-only review tasks (Claude Opus 5) do not acquire write ownership locks and can run concurrently alongside implementation writers.

### Why It Matters
Hierarchical ownership coordinates concurrent writers so multiple agents can safely work in parallel in the same repository when their scopes are disjoint, while preventing conflicting writes on shared files. Durable pending records protect workspaces from accidental corruption after sudden crashes or timeouts, enforcing human or supervisor review before unfinished work is overwritten.

### Implementation Citations
- **Runner**: [`../scripts/provider_runner.py`](../scripts/provider_runner.py) — `normalized_owned_paths()` (lines 109–125), `claims_overlap()` (lines 127–137), `pending_claims()` (lines 181–196), `pending_records()` (lines 139–171), `all_pending_records()` (lines 198–223), `legacy_pending_for()` (lines 225–252), `workspace_state_dir()` (lines 104–107), `provider_lock()` (lines 254–291).
- **Policy**: [`FRAMEWORK.md`](FRAMEWORK.md) — Section "Results and quota handling".

---

## 6. Bounded-Memory Stream Telemetry Parsing

### Problem
Developer CLI tools (Gemini CLI, Claude Code CLI, Codex CLI) output high-volume NDJSON/JSONL event streams during multi-step coding sessions. In complex coding tasks, these streams can grow to tens or hundreds of megabytes. Buffering the entire event history in memory to inspect task progress or parse final outputs causes memory bloat, high GC overhead, and process crashes, while flooding parent agent context windows with raw token streams.

### Decision
Implement bounded-memory streaming telemetry and tail-based terminal parsing:

1. **Incremental Stream Reader**: `_consume_progress_chunk()` consumes raw process stdout in 64 KB chunks (`STREAM_READ_CHUNK_BYTES = 64 * 1024`), cleanly handling line splits and UTF-8 multibyte boundary splits across chunks. Individual lines are capped at 1 MB (`MAX_STREAM_LINE_BYTES = 1024 * 1024`) to guard against runaway lines.
2. **Throttled Summary Telemetry**: Instead of forwarding raw tokens, the runner parses event transitions and emits throttled stderr progress summaries (defaulting to 15s intervals via `STREAM_PROGRESS_EMIT_SECONDS`). It periodically updates `progress.json` with event counts, step indices, and token counters, filtering prompts, model text, reasoning, tool arguments, and tool outputs from progress records.
3. **In-Loop Quiet-Period Heartbeat**: An in-loop liveness write with a 60s default interval clamped to a 5s minimum (`MIN_HEARTBEAT_SECONDS = 5.0`) updates `heartbeat.json` from the runner's poll loop to prove process liveness during long tool executions or network quiet periods, without background threads.
4. **Tail-Based Terminal Output Reading**: Upon process completion, rather than reading the entire transcript, `read_text_tail()` inspects only the final bounded trailing bytes (`FINAL_OUTPUT_TAIL_BYTES = (2 * MAX_STREAM_LINE_BYTES) + STREAM_READ_CHUNK_BYTES` = 2.0625 MiB, roughly 2.1 MB) to extract and validate the terminal completion envelope (`event=result`, `type=result`, or Codex final status).

### Why It Matters
Bounded-memory stream parsing enforces a fixed per-line cap (`MAX_STREAM_LINE_BYTES`) and a fixed tail window (`FINAL_OUTPUT_TAIL_BYTES`), ensuring parsing memory is proportional to those fixed limits rather than to total stream size, regardless of how long the underlying model task runs or how many megabytes of raw stream data are generated. At the same time, it provides real-time liveness and progress visibility to the parent orchestrator without polluting agent context windows.

### Implementation Citations
- **Runner**: [`../scripts/provider_runner.py`](../scripts/provider_runner.py) — `_consume_progress_chunk()` (lines 1722–1764), `read_text_tail()` (lines 86–102), `refresh_progress()` (lines 1766–1803), `maybe_emit_progress()` (lines 1835–1861), `heartbeat()` (lines 1888–1919), `parse_gemini_final_output()` (lines 536–600), `parse_claude_final_output()` (lines 602–646), `parse_deepseek_final_output()` (lines 441–497), constants (lines 25–31).
- **Policy**: [`FRAMEWORK.md`](FRAMEWORK.md) — Section "Provider lifecycle diagnostics".
