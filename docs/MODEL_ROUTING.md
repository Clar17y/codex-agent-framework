# Model routing decision — 22 September 2026

This refresh assigns focused work to GPT-6 Luna, complex native work to GPT-6 Sol, and independent external review to Claude Opus 5.5. Routine implementation uses Gemini with a direct native Luna fallback; DeepSeek is retired from active routing at the user's request. The allocation uses the official guidance and independent evaluations below. It is not a measured ranking of these models on this repository.

## Evidence

OpenAI recommends Sol 6 for complex coding and agent workflows, and Luna 6 for focused, repeatable work. Its Codex documentation describes improvements in coding, factual reliability and communication, while keeping Astra as the most capable option for demanding work across tools. Availability depends on the account and client. [OpenAI model guidance](https://learn.chatgpt.com/docs/models)

| Model | Exact ID | Standard API input / output per million tokens | Documented role |
| --- | --- | --- | --- |
| GPT-6 Luna | `gpt-6-luna` | $0.10 / $0.50 | Focused, high-volume tasks |
| GPT-6 Sol | `gpt-6-sol` | $2 / $10 | Complex coding and agent workflows |
| Claude Opus 5.5 | `claude-opus-5-5` | $4 / $20 | Long-running coding and knowledge work |

Prices are uncached standard API rates, not subscription quota multipliers or measured cost per completed task. Tool charges, cache use, long-context premiums and other processing options can change the bill. Sources: [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), [Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/overview).

Anthropic reports better coding results and efficiency for Opus 5.5, including 66.4% on Terminal-Bench 4.0 and 54.4% on FrontierCode v1.1 in its headline comparison. These are vendor-reported results with different effort settings and some safeguard-triggered fallback models. The comparison includes GPT-5.6 Sol, not GPT-6 Sol or Luna. It supports evaluating Opus 5.5 for demanding review, but does not establish a head-to-head winner among the new models in this framework. [Launch evaluation and methodology](https://www.anthropic.com/claude-opus-5-5)

## Why retire the DeepSeek fallback?

Luna 6's standard input rate is 50% below Luna 5.6's $0.20; output is about 58% below its $1.20. The current uncached rates also undercut DeepSeek V4.1 Flash, including off-peak rates. [Luna 6 pricing](https://developers.openai.com/api/docs/models/gpt-6-luna), [Luna 5.6 pricing](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)

| Standard API rate per million tokens | Luna 6 | DeepSeek V4.1 Flash off-peak | DeepSeek V4.1 Flash peak |
| --- | --- | --- | --- |
| Uncached input | $0.10 | $0.15 | $0.30 |
| Cached input reads | $0.01 | $0.003 | $0.006 |
| Output | $0.50 | $0.60 | $1.20 |

DeepSeek retains a cache-read price advantage. Luna cache writes cost $0.125 per million tokens; requests above 272K input tokens double its input/cache rates and multiply output rates by 1.5. Thus there is no universal cost winner across cache-heavy or long-context workloads. API prices also do not translate directly into Codex subscription usage. The routing choice prioritizes the requested native fallback and removes a provider handoff and its balance prerequisite.

Independent benchmarks support an efficiency gain, not an unconditional capability win. Artificial Analysis reports Luna 6 max at $0.07 per Intelligence Index task versus $0.18 for Luna 5.6 max, while its Coding Agent Index falls from 43 to 41. In the Codex harness, DeepSWE falls from 66% to 64%; other evaluations improve. These are max-effort measurements, not results for our medium-effort workers. [Independent release analysis](https://artificialanalysis.ai/articles/gpt-6-sol-and-luna-push-the-cost-efficiency-frontier)

On Artificial Analysis Intelligence Index v4.3.2, Luna 6 max scores 37 and DeepSeek V4.1 Flash max scores 39. That aggregate does not establish which model completes this framework's coding tasks better. [Luna evaluation](https://artificialanalysis.ai/models/gpt-6-luna), [DeepSeek evaluation](https://artificialanalysis.ai/models/deepseek-v4-1-flash)

OpenAI separately reports Luna 6 max at 66.6% on DeepSWE v1.1 in its own evaluation. Its harness differs from the independent evaluation, so these figures should not be merged or compared as if produced by one test. [OpenAI launch evaluation](https://openai.com/index/introducing-gpt-6-sol-and-luna/)

## Allocation

| Work / role | Model and effort | Reason for the choice |
| --- | --- | --- |
| Primary session | User-selected model | Scope, architecture, integration and final acceptance stay with the chosen primary. |
| Routine implementation | Gemini Flash → GPT-6 Luna medium | Use the requested native fallback directly when Gemini is exhausted, unavailable or blocked, after resolving overlapping ownership. |
| `explorer`, `docs_researcher`, `refactor_auditor`, `verifier` | GPT-6 Luna medium | Bounded investigation and verification match Luna's focused-work role. Escalate material ambiguity. |
| `complex_implementer` | GPT-6 Sol medium | Complex coding merits Sol; this replaces Terra low. |
| `planner`, `test_engineer` | GPT-6 Sol medium | Planning and adversarial test design require judgment; start at Sol 6's documented default. |
| `correctness_gate`, `security_reviewer` | GPT-6 Sol high | Spend additional reasoning on a concrete correctness or security question. |
| Independent external review | Opus 5.5 medium; high with a reason | Use the new Opus in a fresh, read-only review. Medium is its new default. |
| `reviewer` fallback | GPT-6 Astra low | Retain the existing independent native fallback when Claude cannot run. |
| `quality_gate_max` | GPT-6 Sol max | Exceptional, explicitly requested deep audit moves from Luna to the model assigned complex judgment. |

Effort levels are not capability equivalences across models. Raising Luna's effort does not establish that it replaces Sol on difficult contracts. Escalate based on ambiguity, coupling and unresolved failures; keep simple work bounded. The native max audit remains optional, and does not replace the ordinary Opus review route.

## Compatibility and rollout

Claude Code must be at least **2.1.280** for Opus 5.5. Pin the full model ID; the rolling `opus` alias changes over time. Opus 5.5 defaults to medium and can spend more thinking tokens at a given effort than Opus 5. Retain explicit medium/high review settings and assess observed results before increasing them. [Claude Code model configuration](https://code.claude.com/docs/en/model-config), [Opus behavior changes](https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5)

Opus 5.5 requires adaptive thinking and rejects forced tool selection. This framework delegates API conversation handling to Claude Code, starts fresh reviews, and exposes only read/search tools. It does not construct those incompatible API parameters. Progress tracking uses tool lifecycle metadata, so hidden inter-tool prose is not required for liveness. Confirm terminal completion and actual model usage when validating the live route. [Migration guide](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide)

The v9 installer removes the pre-v9 DeepSeek routing entry and no longer installs one. Gemini fallback goes directly to Luna even when an old configuration still contains DeepSeek. Existing provider state, backups, external Codex profiles and credentials are retained. Custom credential environment names remain as filtering metadata, without enabling a provider route. Legacy direct-provider internals remain for compatibility; active policy and setup no longer use them. Earlier migrations, including the exact pre-v8 packaged Opus 5 pin, remain in place. The installer refreshes owned role files and the managed policy and preserves unrelated settings and provider-wide quota state. A custom Claude model remains an operator override; the runner still requires the supported exact review pin. A new Codex task is needed to discover updated native role definitions. Do not infer that an already-running task's role catalog has changed.

## Validation limits

Use the offline suite to verify pins, fallback results, fresh installs, upgrade preservation and idempotence. Live model responses establish account access; a completed file-read probe additionally establishes that tool path under its tested permissions. Neither is a capability benchmark.

Before a later change to provider priority, compare representative bounded fixes, coupled implementation tasks and reviews with known defects under matching inputs and harnesses. Assess accepted-task quality, missed defects, false positives, repair effort, elapsed time and usage together. Historical results in [BENCHMARKS.md](BENCHMARKS.md) remain historical and must not be relabelled as results for these new models.
