# Knowledge retrieval and response scope regression

Base: `54bce57208f7537bf29f143d0821aeb4b75b20d2`.
Scope: shared knowledge retrieval and response policy. No voice, STT, TTS,
endpointing, buffering, provider, speed, recording or latency settings changed.

## Evidence

The Royal Medical call at 14:49 UTC on September 13 used the approved revision
`f069541e-eaf3-5cdd-a4ff-a7c76f25075b`. Its six published sources matched the
compiled draft content lengths; this was not a delayed-publication problem.

- Laser descriptions and a specific intimate-area filler offer exist in that
  release. Retrieval previously omitted the useful laser description and returned
  no evidence for the combined Botox-and-filler question.
- The doctor directory contains more entries than the two general practitioners
  returned by top-k retrieval. A few excerpts must not imply an exhaustive roster.
- Botox, root-canal treatment, and the assignment of endoscopic surgery to
  gastroenterology are not established by the audited sources. No medical facts
  were invented or added to resolve these absences.
- The transcript promised a callback. The saved summary contained a proposed
  follow-up, without an assigned owner or scheduled date; it was not proof of a
  completed callback action.

## Changes

- Retain independent service evidence for short explicit multi-part capability
  questions. Preserve qualifications and decline ambiguous shared filters.
- Retrieve descriptive content for “what kind/type” department questions rather
  than using the department label as the whole answer.
- Attach excerpt-scope guidance: partial evidence is not an exhaustive list or
  confirmation of every service in a compound question.
- For an unfiltered doctor-count request, scan the pinned approved catalogue,
  deduplicate explicitly doctor-titled, evidence-backed names, and label the result
  as a published-name count—not a verified current staffing total. Do not count
  top-k hits, infer professions, drop filters, or read mutable drafts.
- Shared runtime instructions require an actual authorized action-tool result
  before claiming a callback was booked/assigned or promising the team will call.
  This is a model policy, not a deterministic audio-output gate.

## Verification

Focused retrieval, exact-fact, collection and caller-readback groups: 262 passed.
Directory, serving, provider and query-contract groups: 150 passed, 1 skipped.
These groups overlap; do not add them to claim a unique test total.
After review fixes, the combined twelve-file regression run passed 445 tests
with one skipped test. The first full PostgreSQL CI run passed 2,204 tests and
failed one old raw-context assertion; that assertion now uses the same metadata-
aware context parser as Knowledge Studio, while retaining its evidence-order check.

Read-only candidate replay against the approved production revision (not deployed
code, not a browser/audio call):

| Question | Retrieval result |
| --- | --- |
| How many doctors do you have? | 34 distinct explicitly doctor-titled published names, including the separately added dental text; current staff total not verified |
| What kind of laser department are you having? | Laser skin renewal, hair removal and scar-correction source description returned |
| Do you guys provide Botox and filler? | Specific filler-offer evidence returned; no Botox confirmation |
| Do they provide root-canal treatment? | No evidence |
| Is endoscopic surgery under gastroenterology? | No evidence for that relationship |

The replay only changed modules in its isolated diagnostic process; it did not
write production data or replace running workers. Browser-call behaviour still
requires verification after deployment. No latency improvement is claimed.

### Review and generation checks

Review identified four additional regressions, addressed with tests: require all
query variants to preserve an unfiltered count request; retain aggregate counts
within small context budgets; separate retrieval guidance from Knowledge Studio
source cards; and remove matched “type/types” framing from description queries.
Descriptive evidence is now prioritized within the actual four-match voice budget.

Bounded GPT-4o-mini tool-loop smoke tests used the existing authored Royal prompt,
the caller's original query alongside the generated tool query, the approved
revision, and the 3,600-character/four-match runtime limits. Early runs still
offered callback capture because the authored fallback script conflicted with the
policy. The pipeline now supplies explicit knowledge-only capability status when
the actual tool list contains no action tool (including an empty list); connected
or unknown tools are not assumed read-only. The final six-question smoke run
retrieved the three supported topics and did not offer callback capture for the
unsupported topics or promise a callback when asked directly. This checks text
generation, not microphone, audio playback, turn timing, or repeated-call reliability.
Offer validity must still be confirmed from an authoritative dated source; wording
remains model-generated and these smoke tests are not a deterministic fact gate.

## Content follow-up

An authorized clinic representative must supply/confirm the missing service
details and current doctor roster if those exact answers are required. A
limited-time offer without effective dates must not be asserted as currently valid.
Generic extraction completeness is not proof of a complete current staff roster.
