---
name: simplify
description: Simplify recently changed code through reuse, quality, and efficiency reviews, then apply behavior-preserving fixes. Use when asked to simplify, clean up, or reduce complexity in a diff or specified files.
---

# Simplify

Make the selected code easier to maintain without changing its observable behavior. Honor additional focus supplied with `$simplify`, such as a directory or performance concern. A review-only request authorizes findings, not edits.

## Implementer self-check

The full workflow below is coordinated by the primary agent. An implementer assigned a pre-review simplification pass uses only the three review dimensions as a local self-check:

Before handing work over for independent review, inspect your own changes for reuse, clarity, and unnecessary work. Apply only justified simplifications within your assigned files, preserve observable behavior and others' edits, and rerun affected checks after any edits. Include a brief simplification summary (or state that none was warranted) and the resulting check evidence in your handoff. Perform this pass yourself; do not invoke the full $simplify workflow, spawn reviewers, or delegate this pass. This self-check does not replace independent review or verification.

## Establish scope

Read applicable repository instructions. Use the user's explicit files or revision range when supplied. Otherwise inspect `git status --short` and `git diff HEAD` for tracked staged and unstaged changes; inspect relevant untracked source files separately. Handle repositories without HEAD using staged and working-tree diffs. When the tree is clean, use files identified in the conversation. If there is no identifiable target, ask for one instead of selecting arbitrary recently modified files.

Record the starting HEAD, comparison base, and current diff identity. Preserve existing edits and staging. Search surrounding code and callers for evidence, but keep repairs within the selected scope unless a necessary dependent change is explained. Do not commit, push, or broaden this into a repository-wide refactor.

## Review three dimensions

Use three independent read-only passes, concurrently when capacity permits:

- **Reuse:** Find existing helpers that match the actual semantics of new code. Identify proven duplication and name a compatible replacement.
- **Quality:** Look for avoidable state, tangled interfaces, repeated branches, weak types, and abstraction leaks. Prefer clear control flow; preserve comments explaining constraints.
- **Efficiency:** Identify repeated I/O or computation, needless updates, excessive data loading, and resources lacking cleanup. Ground performance findings in a reachable execution path.

Provide each reviewer the same complete scoped diff, relevant new-file content, baseline identity, constraints, and permission to search callers. Require concrete locations, evidence, a proposed repair, and any behavioral risk. Reviewers must not edit files or delegate further. The primary agent checks findings against the source and resolves disagreements before assigning repairs.

## Apply provider routing

For this installation, read `{{CODEX_ROOT}}/agent-framework/docs/FRAMEWORK.md` and the relevant provider skills when invoking their routes. Applicable AGENTS.md rules remain authoritative.

Use fresh `refactor_auditor` agents on Luna medium for reuse and efficiency, and `ask-claude` on Claude Opus 5 at medium effort for quality after its local availability check. These three review tasks may run concurrently within host limits. An active cached Claude quota block goes directly to the framework's disclosed Astra-low reviewer fallback; do not launch the exhausted CLI again. Other Claude unavailability uses the same fallback. Escalate Claude effort only under the framework's stated criteria. Store contracts, saved diffs, and provider output in the active workspace's `.llm-output/`.

Route routine repairs through `ask-gemini`'s local availability check first. An active cached quota block satisfies the route check and goes directly to a fresh Luna-medium implementer; do not contact Gemini for each repair. A new exit-20 quota handoff or unavailable/blocked route also uses the framework's pre-authorized fallback. Preserve partial work and ensure the previous writer has stopped. Route complex bounded repairs to Terra low. Assign explicit file ownership, acceptance criteria, and checks; tell writers they share the codebase and must preserve others' edits. Do not authorize recursive delegation.

## Integrate and verify

Apply only evidence-backed improvements. Reject speculative abstractions, cosmetic churn, and changes whose compatibility cannot be established. Preserve API contracts, error behavior, ordering, side effects, accessibility, and security boundaries. A shorter implementation is not sufficient justification. Report consequential findings that require a separate behavior change rather than silently making it.

Inspect the integrated diff. After repairs, obtain fresh independent Claude review of the final candidate under the same routing policy. High-risk changes also require the separate Sol-high correctness gate after repairs. Have a fresh Luna-medium verifier run repository-required and targeted checks, recording candidate HEAD and uncommitted diff identity. New edits invalidate evidence for affected checks. When no changes are warranted, avoid manufacturing edits or unnecessary test runs.

Finish with a concise account of applied improvements, verification results and limitations, and material deferred findings. Say explicitly when no justified simplification was found.

## Reference

Workflow inspiration: [the linked simplify.ts](https://github.com/emanuelcasco/claude-code/blob/main/src/skills/bundled/simplify.ts). This skill adapts that review-and-repair approach to Codex and the installed provider routing framework.
