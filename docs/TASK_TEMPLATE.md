# Subagent Task Contract

Optional aid for delegated work. Use a concise contract and remove sections that do not apply; direct small fixes do not need this file.

```markdown
## Objective

Describe the observable outcome in one or two sentences.

## Acceptance criteria

- Required behaviour:
- Required error or failure behaviour:
- Complete bounded outcome: own local investigation, implementation, focused validation, routine repairs, command waiting and interpretation, and self-review until criteria pass or a concrete blocker is escalated.
- Autonomous decisions and escalation: resolve ordinary implementation, design, and repair decisions autonomously within the contract using judgment; escalate to the primary only for concrete blockers (material product, requirements, or architectural decisions; ownership conflicts; missing authorization or unavailable prerequisites; or repeated failures requiring reassessment).
- Compatibility requirements:
- Performance or security constraints:

## Risk classification

- Briefly explain the risk and chosen checks. Small, well-understood behavior changes may use focused checks and self-review. Independent review, verification, and scoped correctness gates are options when their added confidence warrants the cost. Preserve explicit repository CI and release requirements.

## Scope

Briefly identify the cause or intended feature behavior, related affected uses, reusable components, and chosen owning layer as applicable. Confirm the assignment covers the intended outcome; raise missing coverage or a better shared implementation with the primary before expanding ownership. A quick search may suffice; no separate scope report or review round is needed.

In scope:
-

Out of scope:
-

## Ownership

You may edit:
- `path/to/owned/file`

You must not edit:
- files owned by other agents;
- unrelated user changes;
- generated or vendored files unless explicitly listed.

## Known partial work and evidence

- Prior baseline, existing diffs, or evidence context if continuing or repairing; omit when starting fresh.

## Repository evidence

- Relevant entry points:
- Relevant tests:
- Applicable `AGENTS.md` files:
- Known conventions or dependencies:

## Validation

Worker owns running, waiting for, and interpreting validation:
- focused test command;
- relevant lint/type/build command;
- repair routine failures before handoff;
- reuse credible matching evidence; rerun only when inputs change, evidence is missing/unreliable, repository policy requires it, or a specific concern warrants it (worker claims alone are not test evidence).

## Handoff

Return a concise report without routine status chatter or raw command dumps:
- completion or blocker status;
- changed paths within assigned ownership;
- concise design rationale and key decisions;
- candidate identity (e.g. HEAD plus scoped diff/status);
- exact commands run and results;
- residual concerns and useful log locations;
- for review repairs: finding IDs, original findings, repair diff, affected behavior, and round number.
```

The parent agent supplies concrete acceptance criteria and explicit non-overlapping ownership before an editing agent starts. Substantial delegated workers own the complete bounded outcome. If two tasks need the same file, sequence their work or assign one owner; forbid overlapping writers.
