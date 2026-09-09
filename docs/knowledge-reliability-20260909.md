# Knowledge reliability correction

## Reproduced production failures

Royal call `8874aaa5-fcb2-5e7b-abfb-185f9b9f3f19` loaded the release containing
the approved text “Dr Kevin is the dental doctor” but returned no evidence.
The compiler correctly retained a person-subject fact; runtime company filtering
required that person subject to equal the company. Independently, “name of” was
treated as a required content term, unlike “Who is the dental doctor?”.

## Changes

- Explicit `owner_company` on a KB, separate from organizational scope and fact
  subjects. Null remains the conservative mixed-company/legacy default. It is
  not inferred from agent names, caller assertions, or website navigation.
- Ownership is frozen in the hashed serving manifest. Editing a draft does not
  change admitted calls or a previously published release.
- Owned sources support both structured facts and approved original text. A
  different selected company receives no evidence from that source. Legacy
  mixed-company sources retain fact-by-fact company filtering.
- Normalize request framing while retaining medical topics, amounts, dates and
  other factual constraints. General doctor directory requests are distinct
  from live appointment availability.
- Flag empty extraction and doctor pages without named entries. Keep short
  plain-text facts valid. These heuristics are not a proof of completeness.
- Before publishing owned KBs, run up to three representative fact questions
  per source through production retrieval against the candidate immutable
  release. A failure prevents the pointer switch. The transaction rolls back.
- Reports are stamped with source fingerprint, release, company and checker
  version. A changed source/owner cannot reuse a green report.
- UI distinguishes “Needs repair”, “Extracted · not verified” and “Sample checks
  passed”. Fact count and character count no longer imply readiness.

## Verification and rollout

Additive migration `20260909_025` must precede new API/worker code. Existing rows
remain untouched. Do not mass-assign ownership from KB titles. For Royal, verify
the administrator-approved single-company ownership against the agent company
scope, save it, then publish through the normal approval endpoint. Do not
rewrite its doctor sentence or amend its immutable historical releases.

Regression tests include healthcare and a separately named trading-company
fixture, equivalent queries, unsupported fees/cancer questions, live slot
questions, tenant/company isolation, pinned versions, report invalidation,
manifest integrity and a failed candidate retaining the previous live pointer.
Run PostgreSQL CI as well as local SQLite tests before release.

## Deliberate limits

- No assertion that every possible question is covered. Publication probes are
  samples; text-only sources remain explicitly unverified.
- Mixed-company legacy KBs are not silently widened or bulk republished.
- Missing live schedules require an appointment-system integration.
- This does not replace the existing rendering/OCR recovery workflow or add a
  speech provider. It fixes the retrieval and readiness boundary.
- No production completion claim until migration, API/worker deployment,
  Royal publication and a new-call verification are recorded.
