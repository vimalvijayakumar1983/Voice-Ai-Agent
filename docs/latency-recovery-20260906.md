# Latency: interruption recovery and stage visibility

## Scope

Preserve the existing voice models, endpointing and approved knowledge. Fix the
unnecessary recovery path exposed by the paired QA call; do not promise sub-600ms
latency from a retrieval-only change.

## Changes

- Normalize leading conversational request framing only in the routing copy.
  Keep branch constraints, company names, negation and the original transcript.
- A typed repeat (phone/address/hours/year) cannot replay an unrelated list.
  Reuse only same-company, matching-request evidence; retry the original lookup
  when needed, preserving branch qualifiers. No extra semantic repair pass.
- Record sequence-bound, content-free controller offsets from scheduling through
  retrieval, speech gate and reply dispatch. Record reply-request to server
  speaking separately. These are NOT browser playback or mouth-to-ear timings.
- Expose only allowlisted numeric timing fields in public call metadata.

## Verification

Regression fixtures cover compound and split stop requests, partial lists,
company changes, failed phone lookups, stale head-office numbers after failed
branch lookups, slow repeats, and preservation of meaningful qualifiers.
Telemetry tests cover stage ordering, sequence isolation, invalid values,
diagnostic callback failure and metadata privacy.

The existing nine-question production QA audio fixture is the paired integration
check. Model/endpointing changes remain deferred until these traces identify the
remaining delay. Production findings are appended after the replay.
