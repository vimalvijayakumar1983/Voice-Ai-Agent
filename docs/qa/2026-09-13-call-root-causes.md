# Directory, grounding-observation and retrieval CPU repair

## Evidence

Human Royal Medical call `7a181322-97ba-5664-a412-32c050a5cabf` completed
2026-09-13 16:59 UTC, duration 194 seconds. Backend and worker were on PR 58.
The transcript contains an unanswered directory count, partial GP lists, an
unsupported Botox detail, a truthful refusal to promise a callback, and a
successful goodbye. Recording was off; this audit does not assess audio quality.
Server response-start latency P50 7592ms, P90/P95 8003ms (eight samples), greeting
2537ms. These are not browser-playback measurements.

## Root causes and bounded corrections

1. Count detection mixed intent, scope, source wording and generated search
   alternatives into one restrictive word check. `listed in your directory`
   failed it. Even after fixing that, synthetic GPT-4o-mini tool-loop QA found
   another failure: the model searched for `number of doctors listed in Royal
   Medical Center directory`, shortening the owner name. Original caller wording
   must remain authoritative, rather than the model's search clue vetoing it.
   Separate operation and role scope. Preserve raw scope queries separately from
   fuzzy expansions. The voice tool supplies its latest caller query for directory
   intent; unknown filters still decline aggregation. Company/tenant/revision
   checks remain. Count the full bounded published entity catalogue, not top-k.
   GP filtering requires explicit evidence, not inferred clinical eligibility.
   No hardcoded clinic name, doctor names, or count is in runtime code.
2. LiveKit stamps assistant messages with generation start, before tool completion.
   VAV used tool completion as a lower timestamp bound, rejected valid answers,
   and discarded late interrupted items after the next caller turn. Use the
   original turn start and bounded historical turn windows to associate the item
   with its own lookup; never assign an old answer to a newer turn. Capture
   explicit missing-information language even in a partly spoken response.
   This is an observation of reported uncertainty, not an entailment verifier.
   Disposition cannot remain fully resolved when that flag is present. A general
   verified search match does not prove that every requested detail was found.
3. Profiling isolated retrieval using the approved corpus plus terminology found
   repeated specialty expansion in overlapping excerpts and expensive fuzzy
   comparisons before cheap rejection conditions. Remove duplicate word work,
   use bounded immutable memoization for pure lexical forms, and move logically
   identical fuzzy rejection checks ahead of similarity calculation. Offload
   terminology planning as well as ranking from the async event loop. Add
   query-plan, source-query, document-preparation and rank timings to call traces.
   No response cache or tenant data cache was introduced.

## Verification

- Regression tests cover literal failed questions; GP list/count; model shortened
  owner wording with authoritative caller intent; generated expansions; foreign
  companies, dates, negation and other filters; pinned release vs newer draft;
  exact late-generation event ordering; and disposition downgrade.
- Exhaustive small-vocabulary fuzzy scoring comparison asserts equivalence with
  the old implementation. Lexical expansion remains set-equivalent.
- Isolated read-only candidate replay on the existing approved release returned
  **34 distinct explicitly doctor-titled names** and **6 explicitly listed GPs**.
  Both counts are published-source counts, not verified current staffing totals.
  No clinic content was edited and no production module or settings were changed
  by the replay (candidate functions existed only in the diagnostic process).
- Controlled retrieval-only replay: directory 31–34ms; GP list/count 21–22ms.
  Laser/filler evidence hashes were unchanged in the CPU optimization comparison.
  Those runs do NOT reproduce the full production tool loop or establish any
  call-latency percentile improvement.
- Bounded synthetic OpenAI GPT-4o-mini text tool-loop QA returned the qualified
  34-name count and six-GP count, and a GP list explicitly labelled incomplete.
  Laser details and supported filler vs unsupported Botox were distinguished.
  This test used the existing authorized provider; it was not an audio call.

## Limits / rollout checks

Not a promise of arbitrary natural-language completeness. Unknown directory
filters continue through general retrieval; the aggregate supports all-doctor
and explicit GP roles, not every possible clinical filtering operation. Source
name aliases can still affect distinct-name counts, hence the spoken caveat.
Undated offers still need clinic verification of current validity. Synthetic
output remained verbose and did not consistently phrase the price as a
historical published offer; this is not claimed fixed here.

No STT/TTS/provider, speech rate, endpointing, recording or deployment settings
changed. New browser-call verification is required after deployment, especially
for interruption association, disposition and real end-to-end latency. Missing
recordings and missing source facts are not silently treated as passing tests.
