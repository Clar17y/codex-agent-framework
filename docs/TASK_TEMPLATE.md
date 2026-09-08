# Subagent Task Contract

Optional aid for delegated work. Use a concise contract and remove sections that do not apply; direct small fixes do not need this file.

```markdown
## Objective

Describe the observable outcome in one or two sentences.

## Acceptance criteria

- Required behaviour:
- Required error or failure behaviour:
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

## Repository evidence

- Relevant entry points:
- Relevant tests:
- Applicable `AGENTS.md` files:
- Known conventions or dependencies:

## Validation

Run:
- focused test command;
- relevant lint/type/build command;
- broader checks when risk, failures, uncertainty, or repository requirements warrant them.

## Handoff

Return:
- files changed;
- concise design rationale;
- commands run and results;
- assumptions, blockers, and residual risks.
- any concrete unresolved question that would benefit from independent scrutiny.
- for review repairs: finding IDs, original findings, repair diff, affected behavior, and round number.
```

The parent agent should supply concrete acceptance criteria and ownership before an editing agent starts. If two agents need the same file, sequence their work or assign one owner.
