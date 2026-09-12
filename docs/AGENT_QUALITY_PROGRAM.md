# VAV Agent Quality Program: Knowledge, Guardrails, Testing, Latency

**Document status:** approved delivery plan
**Baseline reviewed:** `main` at `bbceb9c` (10 September 2026); defects first confirmed at `8f43b94` and re-confirmed on `bbceb9c`
**Supersedes:** the knowledge, guardrail, testing and Smallest.ai sections of `WORLD_CLASS_VOICE_AI_PLATFORM.md`

## 1. Why this program exists

A production call on 10 September 2026 against a medical directory showed that
VAV loses or fails to retrieve information that exists in the approved sources.
The root causes were confirmed in code, not inferred from symptoms:

| # | Defect | Where | Effect |
|---|---|---|---|
| 1 | HTML extractor de-duplicates every text line page-wide | `backend/app/services/website_recovery.py` `extract_readable_text` | Repeated labels such as "General Practitioner" survive once; later records lose their specialty and experience |
| 2 | Compiler silently drops facts it cannot ground in one contiguous span | `backend/app/services/knowledge_compiler.py` `_validated_fact` | After defect 1, later directory entries produce no facts and nothing flags the page as incomplete |
| 3 | Retrieval hides raw text once any structured fact exists | `backend/app/services/knowledge_retrieval.py` `_intent_content`, `_structured_retrieval_content` | A dropped fact is unreachable even though its words are stored |
| 4 | Any two capitalised words in the transcript are treated as a named subject | `knowledge_retrieval.py` `_ENTITY_SEQUENCE`, `_requested_subject_tokens` | "Family Medicine doctor" returns nothing; "family medicine doctor" works |
| 5 | Question words are treated as topic terms that must appear in evidence | `knowledge_retrieval.py` `_QUERY_STOP_WORDS`, `_singular` | "How many general practitioners" requires the word "many" in the source |
| 6 | Full-text index expression does not match the query expression | migration `20260902_018` vs `knowledge_retrieval.py` `_postgres_candidate_source_ids` | Postgres cannot use the GIN index; every turn scans every source's full text |
| 7 | PDF text extraction flattens tables cell by cell | `backend/app/services/pdf_ingestion.py` | Price lists, rosters and fee schedules lose row association |
| 8 | PDF and pasted text are never compiled | `backend/app/api/v1/endpoints/knowledge.py` | Only websites get facts and search phrases; other types are ranked from a headline excerpt |
| 9 | PDF upload and crawl repair call Smallest.ai unconditionally | `endpoints/knowledge.py` `_ensure_remote`, `tasks/knowledge_tasks.py` `_ensure_remote_locked` | A missing provider key breaks ingestion that VAV-native agents never use remotely |
| 10 | Tests run on SQLite | `backend/tests/conftest.py` | The production Postgres retrieval path is not exercised by CI |

The ranker is roughly 1,700 lines of hand-maintained token lists. Each fix for
one failing question has added a rule that creates the next failure. That
pattern ends with this program.

## 2. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Smallest.ai in the knowledge pipeline | Removed. Ingestion and retrieval are local-only | Native agents never read the remote copy. The coupling is an outage risk with no benefit |
| Smallest.ai as a voice provider | Out of scope for this program | Touches 24 backend and 17 frontend files across calls, campaigns, billing and webhooks. Separate decision |
| Vector search | pgvector inside the existing Railway Postgres (extension available, version 0.8.6) | No new service. Same transactions, tenant isolation and backups. Scale is thousands of records per knowledge base |
| Embedding and compilation credentials | One platform-level OpenAI key, tenant override optional later | Ingestion quality must not vary by tenant. Query-time embedding must always work |
| Unit of knowledge | Records, not character chunks | Keeps a name, its attributes and its evidence together for any domain |
| Speculative generation on interim transcripts | On, with a cap of one speculative generation per turn | Removes a few hundred milliseconds per turn; the cancelled-token cost is small against call cost |
| Node-graph flows (Bland, Retell style) | Not on the roadmap | Prompt, rules, knowledge and tests reach parity without a second authoring model |
| Region co-location | Measure first, then move | Provider regions are not visible from code; a per-stage trace decides the move |

## 3. Target architecture

### 3.1 Ingestion (all source types)

```
website / URL / sitemap / PDF / pasted text
        │
        ▼
  Extractor → blocks with structure kept
             (heading path, paragraph, table row, repeated card = one record)
        │
        ▼
  Compiler (OpenAI, platform key) → grounded facts, entities, search phrases per record
        │
        ▼
  Coverage report → entities found vs entities with facts, rejected counts
        │           source is "incomplete" until coverage passes
        ▼
  knowledge_records table
     tenant_id, knowledge_base_id, source_id, subject, heading_path, language,
     text, evidence, tsvector (stored, GIN), embedding (vector, HNSW)
```

### 3.2 Retrieval (per caller turn)

```
transcript ──► lexical: stemmed tsquery on knowledge_records.tsvector ─┐
          └──► vector: embedding(query) <-> knowledge_records.embedding ┘ (concurrent, 120 ms deadline)
                                       │
                          hard filters: tenant, bound + approved KB,
                          language, explicit entity when named
                                       │
                          hybrid score, minimum threshold
                                       │
                 records with source + evidence  ─or─  NO_VERIFIED_KNOWLEDGE_MATCH
```

Lexical results alone are returned if the embedding call misses its deadline.
The old ranker is deleted once the generated question suite passes on the new
path.

### 3.3 Guardrails

Five layers, each enforced where the information exists:

1. **Ingest:** same-host crawl bounds, verbatim evidence per fact, page text treated as data, instruction-like text dropped before indexing, incomplete sources cannot be approved.
2. **Index:** every record carries tenant, knowledge base, subject and language as columns. Similarity search is filtered by these before ranking.
3. **Query:** approved-and-bound filter in SQL, minimum relevance threshold, deadlines on embedding and on retrieval as a whole.
4. **Prompt:** rules rendered in a fixed block; evidence delimited and labelled by source; the existing knowledge policy text retained.
5. **Governance:** approval gated on the question suite; every turn logs returned records, scores and rule firings.

Rules are data: `agent_rules` (and `workspace_rules`) with `rule_type`
(`never_say`, `always_say`, `require_before`, `escalate_when`), `text`,
`exit_action` (`retry_with_instruction`, `transfer`, `end_call`), author and
timestamps. `never_say` rules are checked on model output before synthesis.

### 3.4 Testing

- **Coverage suite:** generated from records at ingest. Name to attribute, attribute to names, counts, paraphrases, per configured language. Runs against Postgres in CI and at approval.
- **Simulation suite:** test case = caller persona, scenario, success criteria. An AI caller runs against the agent through the playground pipeline; a grader returns pass or fail with an explanation. Cases are hand-written, generated from records, or created from a real transcript in one click. Batches run at approval and on a schedule.

### 3.5 Feedback loop

On any transcript turn: mark wrong, supply the correct fact or a rule. One
action creates a reviewed text record, optionally a rule, and a regression test
case.

## 4. Latency budget (speech end to first audio)

| Stage | Target | Levers |
|---|---|---|
| End-of-turn detection | 200 ms | Semantic turn detection already on; tune endpointing delay per language |
| Speech to text final | 150 ms | Streaming recognition; act on interim transcripts |
| Knowledge hook | 150 ms | Indexed records, hybrid query with deadline, cached query embedding |
| Language model first token | 250 ms | Short system prompt, prompt caching, speculative generation on interim transcript |
| Speech synthesis first byte | 150 ms | Streaming synthesis from the first sentence |
| Network | 50 ms | Co-locate Railway, LiveKit and providers after measurement |
| **Total, median** | **under 1,000 ms** | |

The worker already records `last_knowledge_hook_ms`, model first-token, and
synthesis first-byte per turn. The calls page will show the budget against
actuals and name the stage that exceeded it.

## 5. Delivery plan

Each item is one pull request unless stated. Acceptance criteria are the exit
condition; nothing ships without them.

### PR 1. Records, coverage and retrieval wording fixes (pull request #41)

Built on `main` after the serving-revision, compile-outbox and owner-company
work (#31 to #40), and integrated with those mechanisms rather than replacing
them.

- Records are the unit of extraction for every source type: HTML repeated
  sibling components become one record each, HTML and PDF tables become rows
  with column names, pasted text is split into headings, fields, bullets and
  paragraphs. Only whole records repeated verbatim are dropped.
- Compiler prompt treats a record line as one item and emits a fact per field.
- Every compile path (website repair, background text and PDF compilation,
  in-request compilation) stores the records and a coverage report on the
  source. Coverage is shown in each source's quality notes.
- Approval: a source whose AI compilation was requested but did not run blocks
  approval; partial coverage requires `accept_partial_coverage`, which is
  audit-logged. Sources compiled before coverage existed are flagged as
  unmeasured but not blocked, so existing workspaces keep working until they
  are recompiled.
- Ranker: question words and short function words are stop words; a
  capitalised phrase narrows results only when a compiled SUBJECT carries it.
- Migration `20260910_026` aligns the draft-source full-text index with the
  retrieval query expression.

Acceptance: the four-doctor directory fixture yields four whole records; the
generated PDF price table yields one record per row; the five queries that
failed on the 10 September call retrieve the stored fact; existing tests pass.

### PR 1b. Local-only knowledge ingestion

Status: implemented (stacked on PR 1).

Removes the Smallest.ai provider from PDF upload, URL registration, sitemap
discovery, crawl repair, delete and refresh; the knowledge-base provisioning
endpoint; the provider cleanup sweeper for knowledge artifacts; and the
provider-dependent binding rules. Binding a knowledge base to an agent that does
not use VAV retrieval is refused, and an existing binding to such an agent is
never reported as synced. Migration 027 relabels every knowledge base as served
by `vav` and clears remote identifiers.

Acceptance: PDF upload and page repair succeed with no provider key; binding a
knowledge base to an agent that does not use VAV retrieval is refused.

### PR 2. Re-index and completeness for existing knowledge bases

Status: implemented.

- Knowledge-base level re-index (`POST /knowledge/{id}/reindex`, "Re-index all
  sources" in Knowledge Studio): PDFs are re-read from the stored file by the
  background compile worker, pasted text is re-split into records, and website
  pages go through the fenced repair worker with `force_recompile` so an
  unchanged page is still recompiled. Website pages of an approved knowledge
  base are staged, so callers keep the approved text until re-approval.
- Coverage becomes required for approval once a knowledge base has been
  re-indexed (`reindex_requested_at`): from then on a source without a coverage
  report blocks approval instead of being flagged as unmeasured.

Completeness fixes from the Royal Medical verification on 10 September:

- Filter bars, search boxes, pagination widgets and other form controls are
  removed before records are extracted, so "Specialty: All Specialties" or
  "Next" never counts as a missing record.
- A paginated listing (a `rel="next"` link or a "Next" anchor on the same
  host) is followed for up to 12 pages and merged into one source; a card
  layout learned on the first page is still recognised when it appears alone
  on the last page. The number of pages is stored as `pages_followed`.
- A source with no cards, rows, lists or fields is reported as `unstructured`
  ("prose only") rather than "complete: 0 of 0 records".
- Every uncovered record is stored (up to 300) and Knowledge Studio lists all
  of them under "Not captured", instead of three examples.
- A record counts as captured when one fact carries every identifying word of
  each of its fields, ignoring generic nouns such as "department"; words from
  different facts are still never combined.
- The recovery banner reports the source's coverage instead of claiming the
  text was verified.
- Pages are compiled in record-aligned segments of 12,000 characters so each
  strict-JSON reply stays inside the model's output budget; a cut-off reply is
  reported as such instead of surfacing as an invalid document. This was the
  cause of the homepage and doctors "AI compilation failed" results.
- A JavaScript-rendered listing whose "Next" is a button (no link to follow)
  is paginated by the renderer clicking the control and capturing each state,
  up to 12 pages; a click that fails or leaves the site fails the source.

Approval and coverage fixes from the Royal Medical re-index on 11 September:

- The records a first compile pass leaves uncovered are compiled once more in
  a focused pass. Each uncovered record is presented on its own line prefixed
  with the page title and heading path, so a single-entry item such as a
  department name carries the organization and the heading inside its own
  evidence and can become a subject-grounded fact. The pass runs for website,
  PDF and pasted-text sources; a failure keeps the first pass and records the
  reason under `focused_pass`.
- A question that names the owner company ranks the company's own facts ahead
  of person or item facts that merely share the topic word. Directory pages
  carry many short facts whose search phrases mention "location" or "where",
  and with two chunks per source those denser chunks crowded the About page's
  location fact out of the six retrieved chunks. That is why the publication
  retrieval check failed for "What is the location of Royal Medical Center?"
  and blocked approval. The semantic expansion of "location" no longer
  produces a "where" variant, which carried no topic word and matched any
  fact about the subject. The owner is recognised under any page-qualified
  name ("Royal Medical Center Abu Dhabi" for "Royal Medical Center"), since
  each page compiles the same organization slightly differently. The same
  rule (`same_company`) now decides source ownership, the structured-fact
  filter and the exact-fact index filter when an agent carries a company
  scope, so a scope naming the short form still reads a page-qualified
  fact and a scope naming another organization stays fenced.
- Specialty questions match the directory card's word forms: "urology" finds
  a "Consultant Urologist", "pediatric" a "Pediatrician", "psychiatry" a
  "Psychiatrist", and "children", "skin", "heart", "teeth" and "eyes" expand
  to their specialty. In a directory question a framing verb such as
  "treats" is not a fact the card must state; any other question keeps every
  word ("handling fee"). Ordinary words never expand ("clinic" is not
  "clinician"). Before this, every specialty question returned no evidence
  because the strict content rule compared "urology" with "urologist".
- Every knowledge lookup writes one content-free log line
  (`livekit_knowledge_lookup`: turn, result, path, evidence size, timing) so a
  production call can be diagnosed from the agent service logs.
- The publication retrieval check compares words rather than the raw string,
  so presentation punctuation cannot fail a release, and a failure names the
  expected term and the sources that were retrieved instead.
- Knowledge Studio no longer describes indexed sources as provider-confirmed.

Acceptance: an existing knowledge base re-indexes end to end and shows complete
coverage on the Royal Medical doctor directory.

### PR 3. pgvector hybrid retrieval

- Migration enabling `vector`; `knowledge_records` table with stored `tsvector`, GIN and HNSW indexes.
- Embedding at ingest; query embedding at retrieval with deadline and lexical fallback.
- Hybrid scoring, threshold, hard filters. Old ranker removed once the coverage suite passes.
- Tests run against Postgres in CI (service container).

Acceptance: coverage suite passes at 100% on the fixtures and on the Royal Medical knowledge base after re-ingest; knowledge hook p95 under 150 ms on a 5,000-record base.

### PR 4. Guardrails as data

- `agent_rules` and `workspace_rules` tables, API and UI.
- Prompt rendering block; output check for `never_say`; exit actions wired to transfer and end-call.
- Rule firings logged on the call and visible on the Calls page.

Acceptance: a `never_say` rule blocks the phrase in the playground and the firing appears on the call; each exit action verified end to end.

### PR 5. Simulation testing

- Test case model, AI caller, grader, batch runner, results view.
- Generate cases from records; create a case from a transcript.
- Approval shows pass rate; a drop blocks approval until reviewed.

Acceptance: a batch of 50 generated cases runs unattended and reports per-case verdicts with explanations.

### PR 6. Feedback loop

- "Mark wrong" on a transcript turn: correction becomes a record, an optional rule, and a test case in one submission.

Acceptance: a correction made on a call is retrievable on the next call and its test case is in the next batch.

### Latency track (parallel, from PR 1 onward)

- Per-turn stage trace against the budget on the Calls page.
- Speculative generation on interim transcripts, one per turn.
- Region measurement, then co-location.

Acceptance: median speech-end-to-first-audio under 1,000 ms on the playground with knowledge enabled.

## 6. Order and dependencies

```
PR 1 ──► PR 1b ──► PR 2 ──► PR 3 ──► PR 4 ──┐
                                   └──► PR 5 ──┴──► PR 6
Latency track runs alongside from PR 1.
```

PR 4 and PR 5 can proceed in parallel after PR 3. PR 6 writes into all three
and goes last.

## 7. Definition of done for the program

- Every source type is extracted as records, compiled, and reported for coverage.
- Retrieval is hybrid, thresholded, filtered by columns, and independent of caller wording across configured languages.
- Rules are data with exit actions and logged firings.
- Approval is gated on the coverage suite and the simulation suite.
- A wrong answer on a call can be corrected from the Calls page in one action.
- Median turn latency with knowledge enabled is under one second and visible per stage.
