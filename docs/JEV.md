# Jev helpers

Jev supplies semantic code search and advisory code-review briefs across the agent framework. Running `jev-search` is mandatory at the start of substantial coding investigations or architectural exploration before delegating implementation. Running `jev-review` is mandatory on integrated code-changing diffs before requesting independent review or pushing candidate changes. Both helpers use Python 3.11's standard library and TypeSafe's direct System One API. They do not change the Gemini/Claude execution routes or the primary agent's acceptance responsibilities.

### Mandatory triggers and concise exemptions

Adoption of Jev is mandatory and proportionate:
- **Search trigger**: Required at the start of any substantial code search, unfamiliar subsystem investigation, or architectural exploration.
- **Review trigger**: Required on integrated code-changing diffs before independent review or git push.

Permitted concise exemptions:
- Exact symbol, identifier, or literal search where location is already known.
- Tiny localized single-site fixes, documentation-only edits, or configuration edits where behavioral search is not applicable.
- Non-code diffs without logic changes (e.g. markdown, comments, static assets) for review.

When an exemption applies, the caller must record a concise, non-empty reason explaining why Jev was not applicable.

### Preparation ownership and credential isolation

The primary agent or native explorer performs remote Jev preparation before external delegation. Child provider processes (Gemini and Claude CLI) deliberately do not inherit `TYPESAFE_API_KEY`, maintaining workspace authorization boundaries and credential isolation. Claude remains read-only with shell execution excluded.

## Semantic code search

The helper enumerates eligible files and builds bounded source fragments locally, then asks Jev to score relevance. Raw scans and request payloads remain inside the helper; the caller receives a compact JSON response containing original excerpts, locations, scores when available, usage and coverage. Running a verbose grep into the agent's context before invoking Jev defeats that benefit. Exact symbol/literal searches should still use ordinary local tools.

Create the active workspace's `.llm-output` directory before saving evidence. In a macOS / Linux shell:

```bash
mkdir -p .llm-output
python3 scripts/jev_search.py doctor --workspace .
python3 scripts/jev_search.py inspect --workspace . --scope scripts
python3 scripts/jev_search.py search --workspace . --scope scripts --query "Where do we prevent overlapping writers?" --top-k 6 > .llm-output/jev-search.json
```

On Windows (PowerShell), use explicit UTF-8 output:

```powershell
New-Item -ItemType Directory -Force .llm-output | Out-Null
python scripts/jev_search.py search --workspace . --scope scripts --query "Where do we prevent overlapping writers?" --top-k 6 | Set-Content -Encoding utf8 .llm-output/jev-search.json
```

The saved JSON is the file referenced by `task.jev.search.result_path`. Console output alone does not satisfy the gate. `doctor` and `inspect` remain diagnostics, not search evidence.

Use `python3` where needed. In an installed framework, invoke the script under `<codex-root>/agent-framework/scripts/` and pass `--workspace` for the repository being searched. `--config` selects the installed `routing.json`; otherwise the helper looks beside the installed scripts' parent directory. `--scope` is repeatable and confines evaluation to selected workspace paths. `--query-file` accepts a saved question.

Do not interpret an empty semantic result as proof of absence. Incomplete scans, excluded or oversized files, unknown scores, stale sources and exhausted budgets remain visible. Local fallback candidates are explicitly unscored. Broaden the scope or inspect source when the result leaves uncertainty.

The current worktree is read on every search. Cached scores are identified by query, source content and location, workspace, model and question version. The cache under the workspace's `.llm-output/jev-cache/` stores hashes and scores, not plaintext questions, excerpts or credentials. Changed source or changed questions miss naturally. Returned source is rechecked for freshness.

## Configuration and authorization

Set `TYPESAFE_API_KEY` in the invoking process environment. Never store the key in configuration, task contracts, command arguments or source. Ordinary provider children have this key removed, and retained provider artifacts redact its known value. This filtering is defense in depth, not same-user process containment.

Jev defaults to enabled across coding repositories and Git worktrees. With `TYPESAFE_API_KEY` present, ordinary search and review calls need no per-workspace prompt or `--allow-remote`. The default `capabilities.jev` settings in the installed `routing.json` are:

```json
{
  "enabled": true,
  "authorization_mode": "all_workspaces",
  "allowed_roots": [],
  "model": "jev-1.13.0",
  "timeout_seconds": 10,
  "deadline_seconds": 30,
  "max_requests": 8,
  "max_input_tokens": 120000,
  "max_candidates": 96,
  "max_output_chars": 12000,
  "cache_ttl_seconds": 604800
}
```

Fresh installs default to `enabled: true` and `authorization_mode: "all_workspaces"`. To opt out, set `enabled: false`; to restrict remote use, set `authorization_mode: "allowed_roots"` and list exact resolved workspace roots. The root list is ignored in `all_workspaces` mode. Ordinary upgrades add missing defaults while preserving explicit opt-outs and restrictions, including legacy root lists without a mode. `doctor` and `inspect` remain offline.

A denied or unavailable attempt does not disable Jev for the session. Helpers reload configuration on each invocation. Reassess later applicable work and retry when configuration, credentials, workspace, or service readiness changes; do not reuse unavailable evidence as successful preparation. Keep retries bounded and continue locally when an explicit opt-out or missing prerequisite persists.

Eligible source excerpts and the query are sent to `https://api.typesafe.ai/v1/systemone`. Ignore rules, credential-file exclusions, detected-secret filtering, link checks and scope limits reduce disclosure; they cannot establish that arbitrary source contains no sensitive data. Limit scope to the authorized material. Redirects and arbitrary gateway endpoints are not supported.

Processing limits and returned-context limits are separate. Keep returned context small; do not arbitrarily discard relevant files just to make an initial grep quiet. A limited candidate set must be reported as limited coverage. Request input-token counts used for admission are conservative estimates; provider-reported usage is separate. Attempts, including retries, consume the request and estimated-input budget. Late answers are rejected, and network reads also use socket timeouts; cancellation is subject to an in-progress I/O operation rather than an exact wall-clock guarantee. Rate limiting or overload never alters Gemini/Claude quota caches.

## Advisory review brief

Save the candidate diff inside the workspace, including applicable untracked changes, without printing the whole diff into the parent conversation. Then run:

```bash
python3 scripts/jev_review.py --workspace . --diff-file .llm-output/candidate.diff --description-file .llm-output/task-description.txt > .llm-output/jev-review.json
```

On Windows (PowerShell):

```powershell
python scripts/jev_review.py --workspace . --diff-file .llm-output/candidate.diff --description-file .llm-output/task-description.txt | Set-Content -Encoding utf8 .llm-output/jev-review.json
```

Set `task.jev.review.result_path` to this saved JSON and `task.review.diff_path` to the same diff file supplied above. The shared task template includes both `jev.search` and `jev.review`; the adapter validates the stage for its task role and ignores the other stage.

The description is optional. Jev evaluates narrow semantic questions such as changes to authorization, credential flow, ownership coordination, cancellation, persistence and public interfaces. The helper associates signals with parsed diff hunks; counts and locations are computed locally. It returns an advisory review focus and a hash of the exact diff it evaluated. Ambiguous file-header-shaped content inside a hunk keeps that file block local and marks it unknown, even when the text might be literal source content.

A signal is not a verified vulnerability. Low scores do not waive existing review or tests, and the helper never issues a merge approval, modifies code, posts a PR comment or chooses a provider. Missing context, truncated/unsupported diffs and API failures stay unknown. Git paths that use escape sequences are currently reported as unknown. Normal review continues when Jev is unavailable. Recompute the brief when the candidate changes. Supply useful signals and hunk references to the complete review contract, rather than narrowing the reviewer to only Jev-selected areas.

## Mechanical adapter accounting gate

When `workflow.require_jev_evidence=true` is configured in `routing.json` (the default merged on install and upgrade), `provider_runner.py` mechanically enforces Jev accounting before dry-run execution or launching delegated provider tasks.

The adapter gate validates:
- **Implementation tasks (`role=implement`)** require `task.jev.search`:
  - `status: "attempted"`: must specify `result_path` pointing to a bounded (<=1MB), safe regular file (no traversal, symlinks, or hardlinks) inside the workspace containing a valid JSON object generated by `jev_search.py` with `command: "search"` (rejecting `doctor` or `inspect`), a recognized status (`complete`, `partial`, `unavailable`, `error`), and matching workspace.
  - `status: "not_applicable"`: must specify a concise, non-empty `reason`.
- **Review tasks (`role=review`)** require `task.jev.review`:
  - `status: "attempted"`: must specify `result_path` pointing to a bounded (<=1MB), safe regular file inside the workspace containing a valid JSON object generated by `jev_review.py` with `command: "review"` and recognized status. For `complete` and `partial` statuses, `diff_sha256` in the evidence file must strictly match the SHA-256 hash of `task.review.diff_path`.
  - `status: "not_applicable"`: must specify a concise, non-empty `reason`.

The adapter gate rejects missing or invalid accounting before provider launch. Result and dry-run metadata contain `stage` and `decision`, with `reason` for exemptions or `result_path` and the helper's `status` for attempts. Attempts also retain available `coverage`, `diff_sha256`, `error`, `focus_count` and `unknown_count`; partial or failed results remain visibly incomplete.

Save helper results as UTF-8 JSON. UTF-8 with a BOM, as written by Windows PowerShell's `Set-Content -Encoding utf8`, is accepted. UTF-16 output from its plain redirection must be saved again as UTF-8.

With `workflow.require_jev_evidence=false`, the adapter skips Jev validation even if an old contract includes a Jev block. Configurations without this workflow setting retain that legacy behavior until an installation upgrade adds the default.

Instruction-level policy governs arbitrary native subagent calls, while this mechanical gate enforces accounting on delegated adapter executions.

## Verification and limits

Install Git and ripgrep on `PATH`, then run all offline checks with:

```text
python -m unittest discover -s scripts -p "test_*.py"
```

Tests use fake transports and credentials. They cover API handling, output/processing bounds, authorization, source/cache integrity, fallback behaviour, review coverage, credential isolation and installation. A small live smoke check establishes that the deployed API accepts the request and returns useful evidence; it does not validate retrieval quality, security detection or probability calibration across repositories.

Before treating score thresholds as routing policy, evaluate labelled historical changes and code-discovery questions on representative repositories. Measure missed relevant evidence, context returned to the agent, end-to-end latency and actual usage, including cold/cached runs. For review questions, track false negatives per check. Thresholds remain provisional; Jev can be confidently wrong and cannot supply context absent from a diff.

Reference sources checked on 2026-09-24:

- [TypeSafe API](https://docs.typesafe.ai/api), [model limits and pricing](https://docs.typesafe.ai/models), and [known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
- [JevGrep](https://github.com/nassim-arifette/jevgrep) for independent fragment scoring, source evidence and cache identity.
- [sift-light](https://github.com/lightsifter/sift-light) for bounded candidate judgments and visible source coverage.
- [jev-agent-kit](https://github.com/walidboulanouar/jev-agent-kit) and [jev-kit](https://github.com/FlorianRiquelme/jev-kit) for advisory decisions and per-question evaluation.
