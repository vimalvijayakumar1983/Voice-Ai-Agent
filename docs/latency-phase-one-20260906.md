# Latency phase one — behavior-preserving entity resolution

Functional baseline: `dbe2e4524cb0f31589f271da315ccb54797a2071`.

## Changes

- Validated collection requests reach the original collection/index code before
  unused fuzzy entity matching. Query, company fence, pagination, evidence and
  approval/revision checks are unchanged.
- Prepare immutable aliases per agent runtime; refresh preparation if the
  runtime's loaded entry tuple is replaced. No cross-tenant answer cache.
- Prepare caller windows once per alias width, reuse SequenceMatcher's alias
  index, and reject candidates only with conservative upper bounds that cannot
  qualify. Preserve exact scoring, type bonuses, tie order, confidence, margins
  and safe-to-apply decisions.
- Record knowledge_entity_resolution_ms separately (zero on collection bypass).
- Do not change STT, TTS, prompts, endpointing or knowledge content.

## Correctness gates

The frozen baseline resolver lives only in tests/quality/legacy_entity_resolver.py.
Differential tests compare complete EntityResolution values for aliases, spelling
mutations, ambiguous names, duplicate entries, multiple scripts, short strings,
custom thresholds and expected entity types. Collection tests prohibit fuzzy
resolution on that path. Existing conversation tests cover lexicon replacement.

## Same-process production-lexicon benchmark

483 entries, pinned QA revision 8c734ec3-e4ca-5ff9-b5c1-ec7c59713b39.
Five runs per implementation, identical query/lexicon and full-result equality.
One-time preparation: 20.86 ms.

| Query | Baseline median ms | Candidate median ms |
| --- | ---: | ---: |
| Chairman of Al Zaabi Group | 241.87 | 6.18 |
| Natural leadership-list question | 469.31 | 9.31 |
| Short leadership-list request | 197.62 | 5.01 |
| Saed Al Zabi spelling variant | 97.58 | 3.71 |
| Dev Vimal spelling variant | 59.72 | 2.99 |
| Annual revenue | 115.07 | 3.89 |
| Unknown person | 70.59 | 3.16 |

These are CPU-stage measurements, not end-to-end call latency. The list path
bypasses this stage entirely. No sub-600 ms claim follows from this benchmark.

## Bounded production audio comparison

QA agent only: 0d7747ef-6d2a-448a-b7d0-4f9335ea178f.
Use identical cached synthetic PCM, with a pause, list interruption, correction,
unsupported revenue and goodbye. No PSTN, customer recording, booking or payment.

Baseline call: cd9b4950-799b-53bc-9731-ec2b852fe428.
Server-side P50 1446 ms, P90/P95 3464 ms; greeting 576 ms.
The baseline already exhibited a phone-number recovery failure after interruption.
Do not count repeating that behavior as a complete quality pass.

Candidate call measurements and rollout status must be appended after deployment.
Further partial-transcript prefetch or turn-detection tuning requires its own QA
comparison and must preserve interruption, freshness and grounding safeguards.
