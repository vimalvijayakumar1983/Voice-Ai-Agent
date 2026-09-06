# Bounded QA fix and retest — 6 September 2026

## Scope

Keep the existing speech providers/models and QA agent settings. Do not switch
production agents to the native lane. Apply and test four narrowly scoped changes:

1. Optional synthetic caller-tail correlation in replay: a response to an earlier
   fragment cannot complete the whole question. Unrecognized tails fail observation,
   rather than being assigned zero latency. This is a test-only boundary, not a
   production speech detector.
2. Clear the consumed native QA barge-in telemetry latch after final transcription.
3. Route bounded audio checks and caller-memory questions outside business lookup.
   Recall is explicitly attributed to the caller, not verified company data or a
   confirmed booking. Mixed business/action questions do not match this bypass.
4. Native QA knowledge-tool calls use the same authorised company/reference router
   as current VAV, instead of directly querying under a potentially stale company.
   Native turn-taking and response generation remain with the provider.

No new LLM/STT/TTS provider, knowledge-source rewrite, phone assignment, booking,
customer call or external message is part of this test. Generic regression fixtures
use Harbour Group and Sun & Moon centres, not production company-specific facts.

## Validation protocol

Run all backend tests, then one focused browser-admitted synthetic call per existing
QA lane. The `--focused-v2` replay uses nine unchanged cached fixtures: audio check,
chairman, established, leadership, interruption requesting phone, company switch,
company correction, date correction, goodbye. Preserve the earlier cohort; never
pool its timings with this changed implementation/observer.

The Python subscriber still does not measure UAE browser speaker playback. First
voiced audio is not automatically a meaningful answer. `useful_answer_audio_ms`
remains null until a defensible audio/content alignment is available. Completion
of an observed response is not a semantic pass. Review the actual transcript.

The caller-memory bypass covers bounded explicit recollection and call-check
phrasing, not every possible conversational intent or language. Broader acceptance
requires paraphrase/multilingual and real microphone coverage.
