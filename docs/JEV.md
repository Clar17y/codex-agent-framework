# Jev helpers

Jev supplies optional semantic code search and an advisory code-review brief. Both helpers use Python 3.11's standard library and TypeSafe's direct System One API. They do not change the Gemini/Claude execution routes or the primary agent's acceptance responsibilities.

## Semantic code search

The helper enumerates eligible files and builds bounded source fragments locally, then asks Jev to score relevance. Raw scans and request payloads remain inside the helper; the caller receives a compact JSON response containing original excerpts, locations, scores when available, usage and coverage. Running a verbose grep into the agent's context before invoking Jev defeats that benefit. Exact symbol/literal searches should still use ordinary local tools.

```text
python scripts/jev_search.py doctor --workspace .
python scripts/jev_search.py inspect --workspace . --scope scripts
python scripts/jev_search.py search --workspace . --scope scripts --query "Where do we prevent overlapping writers?" --top-k 6 --allow-remote
```

Use `python3` where needed. In an installed framework, invoke the script under `<codex-root>/agent-framework/scripts/` and pass `--workspace` for the repository being searched. `--config` selects the installed `routing.json`; otherwise the helper looks beside the installed scripts' parent directory. `--scope` is repeatable and confines evaluation to selected workspace paths. `--query-file` accepts a saved question.

Do not interpret an empty semantic result as proof of absence. Incomplete scans, excluded or oversized files, unknown scores, stale sources and exhausted budgets remain visible. Local fallback candidates are explicitly unscored. Broaden the scope or inspect source when the result leaves uncertainty.

The current worktree is read on every search. Cached scores are identified by query, source content and location, workspace, model and question version. The cache under the workspace's `.llm-output/jev-cache/` stores hashes and scores, not plaintext questions, excerpts or credentials. Changed source or changed questions miss naturally. Returned source is rechecked for freshness.

## Configuration and authorization

Set `TYPESAFE_API_KEY` in the invoking process environment. Never store the key in configuration, task contracts, command arguments or source. Ordinary provider children have this key removed, and retained provider artifacts redact its known value. This filtering is defense in depth, not same-user process containment.

An existing key alone does not authorize remote source evaluation. For one already-authorized operation use `--allow-remote`. For repeated use, save the following under `capabilities.jev` in the installed `routing.json`, alongside its existing providers:

```json
{
  "enabled": true,
  "allowed_roots": ["C:/dev/my-authorized-project"],
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

Allowed roots match the resolved workspace exactly, rather than authorizing every descendant repository. Fresh installs default to disabled with no roots; ordinary upgrades add missing defaults while preserving existing settings. `doctor` and `inspect` remain offline. Saved authorization and existing session authorization should be reused without repeated permission questions.

Eligible source excerpts and the query are sent to `https://api.typesafe.ai/v1/systemone`. Ignore rules, credential-file exclusions, detected-secret filtering, link checks and scope limits reduce disclosure; they cannot establish that arbitrary source contains no sensitive data. Limit scope to the authorized material. Redirects and arbitrary gateway endpoints are not supported.

Processing limits and returned-context limits are separate. Keep returned context small; do not arbitrarily discard relevant files just to make an initial grep quiet. A limited candidate set must be reported as limited coverage. Request input-token counts used for admission are conservative estimates; provider-reported usage is separate. Attempts, including retries, consume the request and estimated-input budget. Late answers are rejected, and network reads also use socket timeouts; cancellation is subject to an in-progress I/O operation rather than an exact wall-clock guarantee. Rate limiting or overload never alters Gemini/Claude quota caches.

## Advisory review brief

Save the candidate diff inside the workspace, including applicable untracked changes, without printing the whole diff into the parent conversation. Then run:

```text
python scripts/jev_review.py --workspace . --diff-file .llm-output/candidate.diff --description-file .llm-output/task-description.txt --allow-remote
```

The description is optional. Jev evaluates narrow semantic questions such as changes to authorization, credential flow, ownership coordination, cancellation, persistence and public interfaces. The helper associates signals with parsed diff hunks; counts and locations are computed locally. It returns an advisory review focus and a hash of the exact diff it evaluated. Ambiguous file-header-shaped content inside a hunk keeps that file block local and marks it unknown, even when the text might be literal source content.

A signal is not a verified vulnerability. Low scores do not waive existing review or tests, and the helper never issues a merge approval, modifies code, posts a PR comment or chooses a provider. Missing context, truncated/unsupported diffs and API failures stay unknown. Git paths that use escape sequences are currently reported as unknown. Normal review continues when Jev is unavailable. Recompute the brief when the candidate changes. Supply useful signals and hunk references to the complete review contract, rather than narrowing the reviewer to only Jev-selected areas.

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
