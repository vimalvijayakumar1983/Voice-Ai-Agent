# Accepted browser production baseline — September 6

The owner accepted the current VAV experience for broader production-hosted
testing of new agents and companies, with latency work deferred pending
Inworld's engineering reply. This is not a sub-100 ms latency claim or approval
to enable phone routes, recordings, appointment writes or collections campaigns.

## New agents

Creating an Inworld agent now installs `vav-grounded-20260906`: VAV grounded
single-pass, the accepted routing/collections/conversation-state/foundation
features, conditional knowledge-repair transport, Inworld GPT-4o mini and
TTS 1.5 Max. STT uses the existing language-aware resolver. No native-turn QA,
Luna, first-party STT or aggressive endpointing experiment is enabled.

The profile starts in **draft**, with no assigned phone numbers and recording
off. Saving an existing Inworld native profile in the production single-pass
mode installs the same generic conversation features. It does not import a QA
agent's prompt, company directory, knowledge base, credentials or voice.
Existing agents are not migrated silently; other voice providers are unchanged.

## Company onboarding

1. Create the company's knowledge base in Knowledge Studio.
2. Upload/crawl/add approved sources, repair failures and review extracted facts.
3. Approve the release; confirm searchable sources and a serving revision.
4. Create an Inworld agent and select a supported voice and language.
5. For scoped company answering, configure the explicit company names/aliases
   in the agent editor to match approved fact ownership. Enable unmatched-query
   AI repair only when desired; it can add token cost and latency.
6. Bind that company's approved knowledge base and test in Playground.
7. Check factual/paraphrased/unsupported questions, corrections, interruptions,
   company switching and goodbye. Review call evidence before customer rollout.

New knowledge follows the same compiler, review, immutable publication and
call-pinning rules. No code edit is required merely to add another company.
An empty or unapproved knowledge base is not made ready by the voice preset.

## Deployment and rollback

Deploy a CI-passing revision consistently to API, Celery, frontend and LiveKit.
At preflight the production schema was `20260904_024`, all five knowledge bases
had approved serving revisions and lexicons, and no recent call was active.
No schema migration or backfill is introduced by this release.

Keep the previous pointer-aware builds and agent settings for rollback. A mode
change applies to new calls; do not alter an active call's knowledge pin. Keep
the rejected QA B agent out of the production preset. Phone readiness may remain
blocked until verified SIP configuration exists; never paint it green manually.

## Known limits

- Real-world latency and multilingual recognition still require measurement.
- Provider support is investigating U3 final-transcription delays.
- Browser acceptance does not prove carrier/SIP behaviour.
- LiveKit recordings remain unavailable until the governed recording feature.
- Knowledge-grounded answering is not a booking/ERP execution integration.
