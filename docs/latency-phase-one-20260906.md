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

Candidate: 66170dd3-9356-5328-8fbf-47dbb79b1a43, completed, 97 seconds.
Baseline and candidate each have 9 measured response samples. Every synthetic
PCM fixture SHA-256 matched, including the deliberate 700 ms pause and list interruption.

| Server-side metric | Baseline | Candidate |
| --- | ---: | ---: |
| Median response ms | 1446 | 1015 |
| P90 / P95 response ms | 3464 | 3156 |
| Greeting ms | 576 | 494 |
| First exact-fact tool ms | 444 | 20 |
| Established-year tool ms | 314 | 16 |
| Leadership collection tool ms | 913 | 9 |
| Correction exact-fact tool ms | 439 | 19 |

Median improvement in this small paired sample: 29.8%. This is not a production
SLA, a statistically established population improvement, or a mouth-to-ear metric.
The first answer remained slow (3063 -> 3156 ms), despite controller retrieval
falling from 816 -> 90 ms; do not attribute all remaining time to TTS or the DB.
Final-transcript arrival still lagged the received speech-stop event by about
500-1712 ms. Direct-TTS first-byte measurements were 322-360 ms. These boundaries
do not isolate physical end-of-speech, provider endpointing or browser playback.

Both calls retained the same post-interruption phone-number recovery timeout and
stale list repetition. Both had six answered and two unresolved requests. The
approved chairman, founding year, president, unsupported revenue refusal and
goodbye responses matched. Quality remains partially resolved / needs review;
this performance patch does not claim to fix that pre-existing recovery defect.

Validation: 1494 passed, 31 skipped; Ruff check/format passed (242 files).
Focused collection rerun: 43 passed. Existing lexicon-replacement regression
passes after adding preparation invalidation by loaded tuple identity.

Deployed runtime commit: 5006a15. Railway livekit-agent deployment:
eb8bdf89-e3af-46f3-a4f4-ce10ed59dc78, SUCCESS. Both changed runtime files' SHA-256
hashes were checked against the committed archive before the candidate call.
No provider/model/prompt/endpointing/agent/knowledge configuration changed.

Further partial-transcript prefetch or turn-detection tuning requires its own QA
comparison and must preserve interruption, freshness and grounding safeguards.
