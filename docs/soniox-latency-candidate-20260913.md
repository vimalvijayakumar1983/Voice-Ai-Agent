# Soniox latency: isolated test candidate, 13 September 2026

Status: local implementation and offline verification; not a production rollout.
Base: `d18a55749a569782062f3d15d357ad2a3405bba8` (Soniox PR #53).

## Baseline and limits of the evidence

Royal Medical browser call `6dbf24a2-f9e0-517f-b85b-25b774f9013a`
lasted 240 seconds. Its displayed runtime metrics showed median turn latency
4,200 ms, P90 4,571 ms, P95 5,423 ms, and 101,111 aggregate LLM tokens.
There was no recording available. These are server-side call metrics, not a
fresh client-audio benchmark. The aggregate token figure does not prove duplicate
billing or establish how many tokens each model request consumed.

Code inspection confirms Soniox uses the native knowledge-tool agent class with
a direct OpenAI LLM. Its automatic turn hook does not retrieve knowledge. Factual
answers normally need a model request to choose the search, the search itself,
and a second model request to answer. Previous tool outputs stay in chat history.
Existing last-TTFT metrics overwrite earlier request timings within a tool turn.
The evidence is insufficient to blame the entire delay on TTS or to claim a
specific millisecond saving from context changes.

## Candidate

1. Add bounded, content-free per-request measurements on the Soniox pipeline:
   request sequence and turn, instruction/dialogue/tool-result character counts,
   first text and first tool-call chunk times, observed stream duration,
   provider-reported input/cached-input/output tokens, completion/cancellation/error.
   Missing token usage remains null; billing totals are not changed. Public API
   projection includes only validated measurements, not prompts or raw IDs.
2. Add an explicitly opt-in per-agent experiment:
   `agent_metadata.soniox_knowledge_context_window_v1 = true`.
   On each new model request, replace older successful KB result bodies with a
   short instruction to search again if needed. Keep the current and preceding
   user turn's evidence verbatim, all dialogue, all tool-call/result pairs,
   errors and pending/unknown tools. Do not mutate the saved session history.
3. Block the experiment for MCP-enabled or tools-only agents. It introduces no
   extra model, summarizer, provider switch, endpoint delay or output buffering.
   Flag absent/false leaves request context unchanged.

The offline benchmark uses ten synthetic questions, 12,000 instruction characters,
3,600 evidence characters per search and two model requests per question. Total
measured content characters fell from 607,390 to 356,614 (41.3%). This is not a
token-cost estimate, answer-quality evaluation or measured latency improvement.
Provider prefix caching and extra re-searches can affect the net benefit.

## Controlled test before promotion

- Deploy only after review; enable the flag on a QA clone using the same approved
  Royal Medical knowledge revision, model, voice, language and region as baseline.
  Leave the existing production agent's flag absent.
- Use the same recorded caller input for baseline and candidate where authorized.
  Include departments, orthopedics, the doctor's name, laser/beard laser, Botox,
  location, urology and its doctor, thanks/goodbye. Also return to a topic from
  three or more turns ago, change company, correct a name and interrupt a list.
- Compare at least 30 completed factual turns in each arm, separately reporting
  simple answers, knowledge-tool answers, opening and interruptions. Expand the
  sample before treating tail percentiles as stable. Measure from actual end of
  caller speech to first meaningful audible answer, not to an acknowledgement.
- Check evidence/source identity and answer correctness against the approved KB;
  reduced context must not make the model treat previous speech as verified facts.
- Compare request count, provider input/cached tokens, retrieval time, first text,
  first audio, P50/P90/P95 and interruption completeness. Do not add overlapping
  stage durations or equate first text with audible speech.
- Promote only with measured latency benefit and no observed grounding, privacy,
  follow-up or interruption regression. Otherwise disable the flag; the original
  history and retrieval engine remain intact. No production DB migration needed.

Remaining: live A/B measurement, answer-quality evaluation, provider token
reconciliation, greeting optimization if still material. This patch does not
claim to fix all latency or knowledge coverage issues.

Reference: LiveKit supports request-local chat-context copies and preserves
session history separately: https://docs.livekit.io/agents/logic/chat-context/
