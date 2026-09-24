---
name: simplify
description: Simplify recently changed code through reuse, quality, and efficiency reviews, then apply behavior-preserving fixes. Use when asked to simplify, clean up, or reduce complexity in a diff or specified files.
---

# Simplify

Make the selected code easier to maintain without changing its observable behavior. Honor additional focus supplied with `$simplify`, such as a directory or performance concern. A review-only request authorizes findings, not edits.

## Implementer self-check

The full workflow below is coordinated by the primary agent. An implementer assigned a pre-review simplification pass uses only the three review dimensions as a local self-check:

Before handing work over for independent review, inspect your own changes for reuse, clarity, and unnecessary work. Apply only justified simplifications within your assigned files, preserve observable behavior and others' edits, and rerun affected checks after any edits. Include a brief simplification summary (or state that none was warranted) and the resulting check evidence in your handoff. Perform this pass yourself; do not invoke the full $simplify workflow, spawn reviewers, or delegate this pass. This self-check does not replace independent review or verification selected by the primary for a concrete risk or explicitly required by the task or repository.

## Establish scope

Read applicable repository instructions. Use the user's explicit files or revision range when supplied. Otherwise inspect `git status --short` and `git diff HEAD` for tracked staged and unstaged changes; inspect relevant untracked source files separately. Handle repositories without HEAD using staged and working-tree diffs. When the tree is clean, use files identified in the conversation. If there is no identifiable target, ask for one instead of selecting arbitrary recently modified files.

Record the starting HEAD, comparison base, and current diff identity. Preserve existing edits and staging. Search surrounding code and callers for evidence, but keep repairs within the selected scope unless a necessary dependent change is explained. Do not commit, push, or broaden this into a repository-wide refactor.

## Review three dimensions

Cover all three dimensions. They are review lenses, not a required agent count. The primary may cover a small change directly; for substantial work, use one integrated independent review or separate bounded passes only when their distinct questions justify them:

- **Reuse:** Find existing helpers that match the actual semantics of new code. Identify proven duplication and name a compatible replacement.
- **Quality:** Look for avoidable state, tangled interfaces, repeated branches, weak types, and abstraction leaks. Prefer clear control flow; preserve comments explaining constraints.
- **Efficiency:** Identify repeated I/O or computation, needless updates, excessive data loading, and resources lacking cleanup. Ground performance findings in a reachable execution path.

Provide any reviewer the complete scoped diff, relevant new-file content, baseline identity, constraints, and permission to search callers. Require concrete locations, evidence, a proposed repair, and any behavioral risk. Reviewers must not edit files or delegate further. The primary adjudicates the available findings together and groups related repairs by behavior or invariant under one owner. Do not create a repair/review chain for each dimension or comment.

## Apply provider routing

For this installation, read `{{CODEX_ROOT}}/agent-framework/docs/FRAMEWORK.md` and the relevant provider skills when invoking their routes. Applicable AGENTS.md rules remain authoritative.

When delegating independent review, use `ask-claude` under the framework's availability and fallback policy. Use `refactor_auditor` for a separate reuse or efficiency question only when it adds meaningful confidence. Additional roles are conditional, not a mandatory sequence. Store required contracts, saved diffs and provider output in the active workspace's `.llm-output/`.

Small, well-understood repairs may be completed directly with focused checks. For delegated routine repairs, use `ask-gemini`'s local availability check first and follow its configured fallback chain. Preserve partial work and ensure the previous writer has stopped. Use the framework's complex implementation role when warranted. Assign explicit file ownership for the whole related repair batch, acceptance criteria, and checks; tell writers they share the codebase and must preserve others' edits. Do not authorize recursive delegation.

## Integrate and verify

Apply only evidence-backed improvements. Reject speculative abstractions, cosmetic churn, and changes whose compatibility cannot be established. Preserve API contracts, error behavior, ordering, side effects, accessibility, and security boundaries. A shorter implementation is not sufficient justification. Report consequential findings that require a separate behavior change rather than silently making it.

Inspect the integrated diff and run checks appropriate to the changed behavior, honoring repository gates. Reuse credible matching evidence for unchanged inputs. Choose initial review and verification proportionately under the framework policy. Repeat review or verification, or an extra correctness gate, needs a named unresolved question or explicit requirement; neither a high-risk label nor this skill mandates all three.

For repairs after review, check the original finding, repair delta and affected invariants. Do not restart the full review/verification sequence after each edit. A second cycle exposing another defect in the same behavior requires the orchestrator to consolidate the owning component's remaining failure paths into one repair batch before continuing. Follow the framework's review and handoff discipline; never use cycle reduction to waive a real defect or required check. If no uncertainty remains after focused checks and self-review, finish. When no changes are warranted, avoid manufacturing edits or unnecessary test runs.

Finish with a concise account of applied improvements, verification results and limitations, and material deferred findings. Say explicitly when no justified simplification was found.

## Reference

Workflow inspiration: [the linked simplify.ts](https://github.com/emanuelcasco/claude-code/blob/main/src/skills/bundled/simplify.ts). This skill adapts that review-and-repair approach to Codex and the installed provider routing framework.
