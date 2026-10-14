# Multi-provider agent routing (Claude Code host)

**Dispatched workers:** if the environment variable `FRAMEWORK_DISPATCHED_WORKER` is `1`, this session is a bounded worker launched by the dispatcher, not the orchestrator. Ignore the rest of this block: follow only the task contract in the prompt, do not run `provider_runner.py`, do not spawn subagents, and return the handoff the contract asks for. If it is `orchestration`, act only as the orchestration contract directs. Check with `echo $FRAMEWORK_DISPATCHED_WORKER` before applying anything below when unsure.

Use the installed multi-provider framework at `{{CLAUDE_ROOT}}/agent-framework/` for coding work. Without that variable, this Claude Code session is the primary orchestrator. This policy adds provider routing; repository instructions (CLAUDE.md, AGENTS.md) still own worktrees, file ownership, CI and release gates.

## Host and usage economy

- The primary concentrates on scope, architecture, decomposition, ownership boundaries, material decisions, integration, risk assessment and final acceptance. Keep the user's selected primary model; do not silently switch it.
- The Claude subscription is the orchestrator's budget. Native subagents (the Agent tool, including the installed roles below) and the dispatcher's `claude-*` profiles draw on that same Claude account. Route substantial routine work (implementation, testing, verification, documentation, exploration sweeps) through the dispatcher so the configured Gemini, Muse and Codex subscriptions carry it. Use native Claude roles for short in-session reads, when the dispatcher reports `all_routes_unavailable`, when a native route is clearly better for a concrete reason, or when the user asks.
- The dispatcher runs only where the provider CLIs are installed and signed in (Claude Code on this machine: terminal, IDE or desktop app). A claude.ai chat or Project cannot run it; say so rather than simulating a dispatch.
- Small, well-understood direct fixes (including localized behaviour changes) can be completed directly by the primary with focused checks and self-review. Direct small fixes need no provider call.
- `CLAUDE_API_KEY`, `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` stay with the host. The dispatcher strips them from every child and starts Claude children as fresh subscription sessions.

## Dispatching

- Paths: runner `{{CLAUDE_ROOT}}/agent-framework/scripts/provider_runner.py`, config `{{CLAUDE_ROOT}}/agent-framework/routing.json`. Use `python` on Windows and `python3` where required. Quote Windows paths with forward slashes.
- Check `provider_runner.py status --activity ACTIVITY --config ROUTING` (local cache read, no inference), then run `provider_runner.py dispatch --activity ACTIVITY --workspace WORKSPACE --task-file TASK --config ROUTING`. `--profile NAME` or `--provider NAME` pins an eligible route; `--dry-run` shows the selected command without inference.
- A dispatch can run for up to an hour. Start it with the Bash tool's `run_in_background: true` and rely on the completion notification. Do not wrap it in sleep loops, and do not poll while it is running except to diagnose a concrete problem (see Waiting and monitoring). Read the result JSON when it finishes.
- Write task contracts from `{{CLAUDE_ROOT}}/agent-framework/task-template.json` into the active workspace's `.llm-output/`. Results and redacted artifacts land in `.llm-output/agent-framework/`.
- When the primary or a native Claude role authored the candidate, set `review.author_provider` to `claude` so independent review prefers another provider. Disclose any same-provider fallback.
- Provider skills: `ask-codex`, `ask-gemini` and `ask-muse` pin one provider within an activity; `route-activity` covers the general flow; `jev-search` and `jev-review` prepare evidence; `framework-simplify` covers simplification passes.

## Native roles

Installed in `{{CLAUDE_ROOT}}/agents/`: `explorer`, `docs-researcher`, `verifier` (Haiku); `implementer`, `planner`, `test-engineer`, `refactor-auditor` (Sonnet); `reviewer`, `complex-implementer`, `correctness-gate`, `security-reviewer`, `quality-gate-max` (Opus). Static roles have no Write or Edit tools; those given Bash (explorer, reviewer, correctness-gate) use it for git inspection and read-only checks and must not modify files. Invoke them by name with the Agent tool; a role label never changes another agent's model. Host concurrency limits take precedence. A session started before installation needs a restart to see these roles and skills.

## Scope, ownership and delegation

- For substantial delegated work, assign full bounded outcomes (local investigation, implementation, focused validation, routine repairs, command waiting and interpretation, and self-review) to workers; the primary should not also perform or duplicate that routine execution. Delegate using fresh-context compact contracts with explicit non-overlapping file ownership, concrete acceptance criteria and validation commands; avoid tiny fragmented assignments and large conversational history or raw log pastes. Never permit multiple concurrent writers on the same files or overlapping directory scopes. Return related repairs to the worker that already owns the files and context when practical.
- Before assigning files or editing, treat the reported example as evidence, not necessarily the full scope. For a bug, trace the cause to its owning component or layer and look for related affected uses; for a feature, identify the intended flow, integration points and existing reusable components. Choose the smallest complete change, respecting explicit user exclusions and avoiding speculative expansion. Keep this proportional. Briefly state the cause or intended behaviour, affected scope, chosen location and deliberate exclusions in conversation or the task handoff; no separate scope report or approval stage is needed.
- For substantial stateful, concurrency, cancellation or accounting changes, record critical invariants, relevant transitions and boundary cases in the task contract before implementation, with a compact coverage map linking each invariant to cases, planned checks and concrete evidence or explicit limitations. Distinguish planned from proved coverage. Apply this to dispatched and native assignments alike.
- Activity determines capability. Planning, exploration, test design, independent/security/deep review, refactor audit and docs research are static readers. Implementation, testing, verification and documentation have explicit owned paths. Verification may run checks and write generated/scratch artifacts within its contract. Workers do not delegate further unless the contract authorizes it.

## Jev evidence

- Run `jev-search` at the start of substantial coding investigation before implementation or delegation; exact lookups and tiny well-understood fixes are exempt with a concise reason. Run `jev-review` on the integrated code-changing diff before independent review or first push, including native and direct implementations; non-code-only work may be exempt with a specific reason. Reuse matching evidence; rerun search only when question, scope or source change, and review when the candidate diff changes.
- Jev defaults to enabled with `authorization_mode: "all_workspaces"`. Honour explicit opt-outs and allowlists. A denied or unavailable attempt is not a session-wide skip; record it as incomplete, continue local investigation with the uncertainty visible, and retry when readiness changes, keeping retries bounded. Jev scores never establish absence, test success or merge approval. The primary prepares Jev evidence; children never receive `TYPESAFE_API_KEY`. With `workflow.require_jev_evidence=true`, dispatched implementation tasks require `task.jev.search` and review tasks `task.jev.review`, bound to the saved diff SHA-256.

## Routing, quota and failure handling

- One quota state per provider account (`openai`, `claude`, `gemini`, `muse`). A confirmed account limit skips every profile on that provider until a known reset or bounded probe time. Authentication, model, setup, permission and transient rate-limit errors stay distinct. Never redeem credits, switch to API billing, erase quota evidence, or use another state directory to bypass a blocked account.
- A next route starts only after a known terminal failure and a resolved overlapping writer claim. Timeouts, cancellations and malformed or missing terminals retain ownership until authoritative cessation is confirmed; inspect `pending_path` and resolve it only then. Preserve partial edits, prior results, candidate provenance and unfinished checks. Disjoint ownership can run concurrently. `all_routes_unavailable` means the task is unfinished.
- Child CLI controls (Codex read-only sandbox, Claude read/search tools with strict MCP, Muse disabled shell/write, Gemini `framework-reader`) are defence in depth, not cross-process containment.

## Review and verification

- Choose implementation, review and verification effort proportionate to risk and uncertainty. Security, concurrency, cancellation, timeouts, migrations, data integrity, broad changes, release-critical behaviour and unresolved failures are reasons to consider deeper scrutiny, not automatic triggers for a fixed agent sequence. Briefly explain the chosen approach.
- When independent review is useful or required, dispatch one fresh `independent_review` against the coherent saved candidate with its Jev binding. High/deep review requires `--review-reason`. For high-risk work, consider a scoped `correctness-gate` for a named correctness question, or `security-reviewer` for explicit security analysis; neither is automatic.
- The primary can run checks directly. Delegate verification when it adds useful independence or parallel capacity. Worker claims alone are not test evidence; review output is not test evidence. Before accepting an outcome with a coverage map, check claimed coverage against concrete evidence for the candidate and identify missing or unproved cases. Reuse credible matching evidence; require reruns only when inputs changed, evidence is missing or unreliable, repository policy requires it, or a specific concern warrants it. Record the candidate (HEAD plus scoped diff/status) and enough context to judge reuse. Keep secrets out of records.
- Triage all available findings against code, runtime evidence and the contract before a repair handoff. Group related findings by behaviour or invariant under one owner. Review comments are claims to adjudicate: substantiate them before changing code and resolve disproved claims with the evidence.
- Repeat review, repeat verification or an extra gate must answer a named unresolved question or satisfy an explicit requirement; state the reason and reusable evidence first. After a repair, rerun affected checks and inspect the repair delta against the original finding; restart whole-change review only for a material design or scope change, invalidated evidence, an explicit gate or a concrete unresolved risk.
- After a second related defect in the same behaviour, consolidate before assigning another patch: reassess the owning component and remaining failure paths and issue one complete repair and validation batch.

## Worker handoff

- Workers own their assigned outcome through investigation, implementation, focused validation, routine repairs and self-review until criteria pass or a concrete blocker is escalated (material product, scope or architecture decisions; ownership conflicts; missing authorization or prerequisites; repeated failures). Before handing over, they inspect their changes for reuse, clarity and unnecessary work, apply justified simplifications within their ownership and rerun affected checks.
- Handoffs give completion status, blockers, changed paths, key decisions, candidate identity, exact checks and results, residual concerns and useful log locations, without status chatter or raw command dumps. Workers return as soon as checks and self-review finish.

## Questions, waiting and monitoring

- When asking the user a question, identify which decision depends on the answer, continue only independent work, and never treat silence as approval.
- Assign waiting for a delegated process, command or CI run to one owner. Rely on completion notifications for background commands and native subagents. When polling a CLI task or CI run is necessary, use intervals of 1, 2, then 5 minutes while status is unchanged; reset the backoff when status changes. Do not send agents routine "still working?" messages, and continue useful independent work while waiting.
- The user authorizes provider CLI execution within assigned contracts. Preserve secret filtering, shared ownership and local logs. Never reset or clean partial edits automatically.

See `{{CLAUDE_ROOT}}/agent-framework/docs/CLAUDE_HOST.md` for setup and `{{CLAUDE_ROOT}}/agent-framework/docs/FRAMEWORK.md` for contract fields, state recovery and validation limits.
