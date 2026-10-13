# Model routing rationale

The editable `profiles` and `activities` in [routing.example.json](../routing.example.json) are recommendations for balanced quality, speed and subscription usage. They are not measured performance or quota weights for this repository. All four providers are eligible for every activity by default. OpenAI Sol 6.1 handles general judgment; Gemini Flash medium starts routine implementation; Muse Full and Claude Opus appear early for complex or long-horizon work; Luna, Haiku and regular Wonder fill bounded roles; Astra is an exceptional deep-review option. Independent review prefers another provider than the author.

The user subscriptions are Codex Pro, Claude Pro, Google AI Pro and Muse Power. Account allowances and reset behavior differ by provider and can change. API prices are secondary and cannot be converted to subscription quota. The dispatcher conservatively treats a confirmed usage limit as provider-wide, even if the error mentions one model. It does not redeem credits or switch billing modes.

[OpenAI model guidance](https://developers.openai.com/api/docs/models/compare) describes Sol 6.1 as near Astra on complex work at a lower API price and Luna as a focused high-volume model. [Independent Sol comparison](https://artificialanalysis.ai/articles/gpt-6-1-sol-replaces-gpt-6-sol-after-just-7-days-with-near-astra-intelligence) finds Sol max close to Astra on its intelligence and coding indexes; xhigh coding can differ. Those results do not establish medium-effort performance or subscription usage here. Astra remains a current capable model, retained for exceptional depth.

[Artificial Analysis on Muse](https://artificialanalysis.ai/articles/muse-wonder/) reports strong Wonder and Wonder Full coding results in product harnesses, mostly at max effort; Wonder Full's long-horizon advantage remains provisional. [Its Wonder versus Gemini comparison](https://artificialanalysis.ai/models/comparisons/muse-wonder-vs-gemini-3-8-flash) uses Wonder max and Flash high, not medium versus medium. [Google's Flash model card](https://deepmind.google/models/model-cards/gemini-3-8-flash/) and [Anthropic's Sonnet](https://www.anthropic.com/claude-sonnet-5-5), [Opus](https://www.anthropic.com/claude-opus-5-5) and [Haiku](https://www.anthropic.com/claude-haiku-5-5) reports use different harnesses and effort settings; scores should not be treated as a universal ranking. [Independent Haiku versus Luna comparison](https://artificialanalysis.ai/models/comparisons/claude-haiku-5-5-vs-gpt-6-luna) shows meaningful differences on its max-effort tests, so the two small models are not interchangeable.

The defaults should be evaluated against real tasks and account allowance. An early Muse release has limited mature firsthand evidence. Neither benchmark scores nor API prices prove the available subscription throughput, provider authentication, or live tool permissions.

## Default priority chains

These are editable in `routing.json`; each named profile pins a model and effort. `>` means try the next profile only after a settled failure and safe ownership handoff. Every row includes all four providers, even when only the most likely routes are early.

| Activity | Ordered profiles |
| --- | --- |
| orchestration | openai-sol > claude-opus > muse-full > claude-sonnet > gemini-flash > muse-wonder |
| planning | openai-sol > claude-opus > muse-full > claude-sonnet > gemini-flash-high |
| exploration | openai-luna > claude-haiku > gemini-flash > muse-wonder |
| implementation | gemini-flash > muse-wonder > claude-sonnet > openai-sol |
| implementation_complex | openai-sol-high > muse-full > claude-opus > claude-sonnet-high > gemini-flash-high |
| implementation_long_horizon | muse-full > claude-opus > openai-sol-high > claude-sonnet-high > gemini-flash-high |
| test_design | openai-sol > claude-opus > muse-full > claude-sonnet > gemini-flash |
| testing | openai-sol > claude-sonnet > gemini-flash > muse-wonder |
| verification | openai-luna > gemini-flash > claude-haiku > muse-wonder |
| independent_review | claude-opus > openai-sol > muse-full > claude-sonnet > gemini-flash-high > muse-wonder |
| security_review | claude-opus-high > openai-sol-high > muse-full-high > claude-sonnet-high > gemini-flash-high |
| refactor_audit | openai-luna > claude-sonnet > gemini-flash > muse-wonder |
| documentation | gemini-flash > openai-luna > claude-haiku > muse-wonder |
| docs_research | gemini-flash > openai-luna > claude-haiku > muse-wonder |
| deep_review | openai-astra > claude-opus-high > muse-full-high > openai-sol-high > gemini-flash-high |

## Model roles and evidence limits

| User model | Default use and evidence limit |
| --- | --- |
| GPT-6.1 Sol | General planning, complex implementation and review fallback. Independent [v4.3.2 evaluation](https://artificialanalysis.ai/articles/gpt-6-1-sol-replaces-gpt-6-sol-after-just-7-days-with-near-astra-intelligence) puts **max** one intelligence point and two coding points below Astra; xhigh coding scores one point above Astra. These are not medium-effort measurements. |
| GPT-6 Astra | Exceptional deep review. [OpenAI guidance](https://developers.openai.com/api/docs/models/compare) still calls it the most capable demanding-work model; it is not deprecated. |
| GPT-6 Luna | Bounded exploration, verification and audit. Its [FrontierCode 1.1](https://artificialanalysis.ai/articles/frontiercode) max score was 42% on a common harness, with Luna medium also used as qualitative judge; task mix matters. |
| Claude Opus 5.5 | Independent review and long-horizon alternatives. Its 52% FrontierCode max score led that common-harness set, but coding completion is not a direct review-quality test. |
| Claude Sonnet 5.5 | Fast scoped implementation and review alternative. [Anthropic's TerminalBench 4](https://www.anthropic.com/claude-sonnet-5-5) 70.6% was a high-effort vendor setup; it does not universally outrank Opus. |
| Claude Haiku 5.5 | Bounded research and verification. The [independent Haiku/Luna comparison](https://artificialanalysis.ai/models/comparisons/claude-haiku-5-5-vs-gpt-6-luna) shows task-dependent differences rather than interchangeable small models. |
| Gemini 3.8 Flash | First routine implementation and documentation route for speed/subscription balance. Its [model card](https://deepmind.google/models/model-cards/gemini-3-8-flash/) reports strong DeepSWE and earlier TerminalBench results, with TB4 materially lower; harness versions cannot be mixed. |
| Muse Wonder | Routine implementation alternative. [Independent product-harness results](https://artificialanalysis.ai/articles/muse-wonder/) are promising mostly at max effort; medium and Power-account throughput remain unmeasured here. |
| Muse Wonder Full | Complex/long-horizon alternative. The same independent report shows a stronger product-harness coding index than regular Wonder at max, while [Meta's model card](https://research.meta.ai/static/muse-wonder-model-card) uses xhigh for SWE-Marathon. Long-horizon superiority remains provisional. |

On one independent [FrontierCode 1.1](https://artificialanalysis.ai/articles/frontiercode) common harness of 100 private maintainer tasks with three attempts each, max scores were Opus 52%, Sol 49%, Astra 47%, and Luna 42%; Muse was not evaluated there. The 58% Opus *pass rate* is a different metric. API cost per task in that setup was Sol $0.81 versus Astra $4.22, not a Codex subscription weight. Early [Techmeme-collected release-day comments](https://www.techmeme.com/?app=1) include praise for Wonder Full on hard tasks and mixed reactions; the original posts were inaccessible during research and do not establish mature consensus or any account's quota.

Task-specific evidence can cut against a broad model ranking. [Bug Hunt Bench](https://github.com/phuryn/bug-hunt-bench) uses 105 planted bugs in two repositories, vendor CLIs and blind grading: at max, three-run mean fixes were Sonnet 51.3, Astra 45, Sol 44.3 and Opus 41.7; at medium, Opus 30.3, Sol 29 and Sonnet 23.3. It is a bug-fixing harness, not a pure static-review test, and Muse was absent.

Self-selected developer reports are useful leads rather than consensus: one small [Sonnet/Opus report](https://www.reddit.com/r/ClaudeCode/comments/1x2ciix/difference_between_sonnet_55_and_opus_55_for/) found Sonnet competitive across eight implementation tasks but weaker at finding bugs; a [24-task medium-effort Sol comparison](https://www.reddit.com/r/codex/comments/1x46r64/i_ran_gpt_61_sol_against_gpt_6_sol_on_24_tasks/) reported nine wins, ten ties and five losses versus its predecessor, while [other users reported latency concerns](https://www.reddit.com/r/codex/comments/1x4br68/gpt61sol_is_slooowwww/). A [multi-file Gemini task report](https://www.reddit.com/r/google_antigravity/comments/1w6h936/google_claims_are_correct_guys/) was positive, whereas a [C++ author described false bug claims](https://www.reddit.com/r/GeminiAI/comments/1work8i/gemini_38_flash_keeps_inventing_bugs_in_real_c/). [Haiku users](https://www.reddit.com/r/ClaudeCode/comments/1x132yy/is_haiku_55_good_enough_to_be_your_main_coding/) emphasize bounded, well-specified work and latency, while noting harder integration and ambiguous tasks. None of these samples establishes a universal model order.
