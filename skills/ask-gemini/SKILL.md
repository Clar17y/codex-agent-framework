---
name: ask-gemini
description: Delegate routine implementation or explicitly requested Gemini work to the installed agy CLI, with pre-authorized Luna medium fallback when the Gemini route is exhausted, unavailable, or blocked.
---

Use `{{CODEX_ROOT}}/agent-framework/scripts/provider_runner.py` with Python 3.11 or newer (`python3` on macOS; `python` on Windows). Read `{{CODEX_ROOT}}/agent-framework/docs/FRAMEWORK.md` for the contract and command. Invoke role `implement` from the explicitly authorized active workspace/worktree. The configured model is exactly `gemini-3.8-flash-medium`.

Provide concrete acceptance criteria, owned paths, validation and applicable instructions. Gemini owns the complete local implement/test/repair loop within those paths. Do not invoke it in a canonical checkout when repository policy requires a provisioned worktree. Scope and write permissions must be supported by the user's task. The user explicitly authorized the adapter's CLI bypass mode. Codex sandbox and automatic approval review still apply.

Use this route when the primary chooses delegation; small, well-understood fixes can be completed directly without invoking this skill. The parent chooses review and verification depth according to risk and uncertainty. Workers give concise handoffs; separate process records and additional gates are optional unless explicitly required.

Read the adapter's structured result and evidence files. On exit 20 / `fallback_required`, immediately delegate the same contract and partial changes to native `implementer`, explicitly `gpt-5.6-luna` / `medium`; no additional user approval is needed for the already authorized task. Keep all remaining routine work on this route while the quota cache is active. Only one writer may own the files, and Gemini must have exited before Luna starts. Luna must inspect partial edits before continuing.

Cached quota expires at a known reset or a bounded probe time; a probe is not a claim that quota reset. If the checked Gemini route is unavailable or blocked (including `blocked_pending_run`, authentication, model, launch, timeout, or provider permission failures), report the actual reason and use native `implementer`, `gpt-5.6-luna` / `medium`, without asking the user again. Do not misclassify these failures as quota exhaustion or clear unresolved pending state. Confirm any prior writer has stopped before handing over overlapping files; a pending run in another workspace does not block a fresh Luna task with non-overlapping ownership. Codex permissions and safety review still apply. Escalate hard implementation defects through the complex-implementation route. Never claim provider success establishes test success; assess the candidate and its check evidence, using independent review or verification when warranted.

## Scope confirmation

Include this guidance in delegated implementation contracts and preserve it in fallback handoffs:

Before implementation, confirm that the assigned change addresses the underlying cause or intended feature behavior and relevant affected cases. Look for related uses and existing reusable components, keeping investigation proportional. If the assignment misses coverage or a better shared implementation would cross your owned files, tell the primary before expanding ownership; do not silently deliver a known-incomplete fix. Validate representative affected cases and mention deliberate exclusions concisely. This is a lightweight scope check, not a required report or extra review round.

## Implementation handoff

Include this requirement explicitly in the task contract and preserve it in any Luna fallback handoff:

Before handing over implementation, inspect your changes for reuse, clarity, and unnecessary work. Apply justified simplifications within your ownership, preserve observable behavior and others' edits, and rerun affected checks if needed. Include a short summary and check results in the handoff. Perform this pass yourself; separate review or verification is chosen by the primary according to risk.

## Concurrent runs

Gemini tasks may run concurrently in separate worktrees or in the same workspace with disjoint `owned_paths`. Declare literal files or directories in each contract, without glob patterns; a directory includes its descendants, and empty ownership conservatively claims the entire workspace. Overlapping active or uncertain claims block the conflicting task; unrelated files remain available. The adapter holds a short lock only while coordinating claims. Use the result's `pending_path` to locate unresolved evidence. Account quota remains shared. Legacy pending records are preserved and scoped by their recorded workspace or log path; records without file ownership protect their entire workspace, and unknown workspace ownership remains blocked pending investigation. Claude reviews run concurrently without these Gemini gates. Do not run an old adapter copy concurrently with the updated adapter.
