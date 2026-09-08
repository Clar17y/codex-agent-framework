# Historical v3 evidence (supplied archive)

These are the supplied framework author''s historical measurements, preserved without re-running them. The current v4 routing is in ../README.md and GLOBAL_POLICY.md. Gemini and Claude were not included in these comparisons.

# Benchmark Evidence

## Established implementation routing

| Model | Hard implementation score | Completion time |
|---|---:|---:|
| Luna medium | 28/32 | 2m15s |
| Terra low | 31/32 | about 2m15s |
| Terra medium | 31/32 | about 2m30s |
| Sol low | 30/32 historical, 31/32 fresh | 3m43s historical |
| Luna max | 32/32 | 31m52s |
| Astra low | 31/32 twice | 2m58s mean combined |

Astra low was faster than the fresh Sol-low control but did not beat Terra low or justify its higher price as a routine implementer. Astra medium repeated the same missed error-code edge case.

## Difficult independent review

| Model | Result |
|---|---:|
| Astra low | 30/30 twice, 41.4s mean |
| Sol low | 26/30 fresh, 30/30 historical |
| Luna medium | 26/30 |
| Terra low | 26/30 |
| Terra medium | 26/30 |

Astra low was the only model to produce consecutive perfect hard-review runs and becomes the difficult reviewer.

## Parent orchestration

| Parent | Workers | Score | Time | Repair loops |
|---|---|---:|---:|---:|
| Astra low | 2 x Luna medium | 58/60 | 173s | 1 |
| Astra medium | 2 x Luna medium | 58/60 | 213s | 2 |
| Sol high | 2 x Luna medium | 60/60 | 296s | 1 |

Astra low was 41% faster than Sol high but introduced a final-timeout liveness hang. Astra medium made the same mistake and was slower. Sol high remains the correctness-first parent and final gate.

## Resulting policy

- Astra low: fast parent and difficult independent reviewer.
- Luna medium: routine implementation and verification.
- Terra low: complex bounded implementation.
- Sol low: planning and adversarial test design.
- Sol high: correctness-first parent, security review, and high-risk final gate.
- Luna max: exceptional exhaustive gate only.
- Astra medium and Terra medium: no default role.

These results are workload-specific. Re-run representative benchmarks after material model, harness, repository, or pricing changes.
