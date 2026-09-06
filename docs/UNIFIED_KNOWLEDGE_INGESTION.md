# Unified knowledge ingestion

## Scope

Compiler `vav-knowledge-compiler-13` provides one grounding and retrieval-document
contract for extracted website pages, PDF text (including OCR), and pasted text.
Compilation happens during ingestion, not during calls. No speech, model routing,
latency settings, agent scope, or approved live knowledge is changed by this release.

## New sources

- Website crawling continues to use the background recovery/crawl worker.
- PDF upload extracts/OCRs before compilation. Only after successful compilation
  does it enter the existing provider upload/reservation/cleanup workflow. Original
  PDF bytes remain in `file_content`; original extracted text remains in `raw_content`.
  The provider retains its searchable original PDF artifact, while VAV uses the
  compiled retrieval document and facts. Compilation is not provider-index readiness.
- Pasted text compiles before creating an indexed draft. Identical name/content
  retries reuse the source instead of creating duplicate rows. Different text is
  never silently substituted for a same-named source.
- Both uploaded paths use the configured tenant OpenAI credential, with the existing
  platform fallback policy. AI work runs before taking the KB publication lock;
  mutation preconditions are rechecked afterwards.

The shared mode selector is available for website, PDF, and text:

| Mode | Behaviour |
| --- | --- |
| Automatic | Attempts source-checked AI facts, including short FAQs. If unavailable, retains searchable text with a visible warning. |
| Fast | No AI call. Searchable source text and deterministic contacts; no claim of company-attributed facts. |
| AI/source-checked | Requires OpenAI. Compilation failures stop the mutation; invalid individual facts are excluded and warned about. |

Original source content remains in the retrieval document as well as separately
stored raw text. The structured representation augments rather than replaces it.
AI facts must pass the existing evidence, value, and subject-attribution checks.
AI does not prove semantic correctness or exhaustive coverage: review remains necessary.

Large extracted documents are passed through bounded, overlapping AI segments
rather than truncating the input after 120,000 characters. All original text is
retained. Returned facts are still not an exhaustive inventory, and missing facts
must not be treated as authoritative proof that information does not exist.

## Existing sources and review

`POST /knowledge/{kb_id}/sources/{source_id}/compile` upgrades an existing PDF/text
source in place. The UI calls this **Structure with AI** and discloses extraction
charges. It reads preserved extraction (or legacy plain content), uses the shared
compiler, and rejects a result if the source changed while inference was running.
Unchanged successful compilation is reused. It does not modify provider artifacts.
Unreadable sources with no original text require re-upload/OCR; this action does
not pretend to repair missing bytes or a failed provider index.

`GET /knowledge/{kb_id}/sources/{source_id}/preview` is tenant-authenticated and
source-scoped. The lazy UI shows extracted original text plus fact/evidence pairs
as escaped text. It does not expose original files or credentials and does not load
every source's full contents into the inventory response.

Changes remain drafts. Existing immutable serving revisions and their lexicons
remain active until approval publishes a replacement. Compilation errors preserve
the previous source and live revision. No mass recompile or automatic approval is
performed by deployment. Pasted text remains VAV-native only; existing Smallest.ai
provider binding restrictions and PDF cleanup barriers remain in force.

## Operational limits

PDF/text compilation currently runs in the upload/recompile request. The UI shows
the work-in-progress state and retains entered input; large documents can take
longer, and requests interrupted by infrastructure timeouts may need retrying.
This release does not add a durable asynchronous upload job or resumable OCR.
Website discovery still respects crawl limits, robots rules, authentication, and
access restrictions. It cannot guarantee access to every page of every website.

## Verification

Automated tests exercise PDF/text compiler parity, source/binary preservation,
rejection of unsupported facts, long-input tail coverage, deterministic fallback,
in-place legacy upgrades, retained approved revisions, inference failure before
remote upload, concurrent edit protection, idempotent text retries, preview
authentication/ownership, and UI mode/transport/review wiring. Provider inference
is mocked in these tests; they are not live voice-quality or paid inference tests.
