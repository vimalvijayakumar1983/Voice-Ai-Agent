"""Safe, retrieval-ready PDF ingestion with selective OCR fallback.

Pages are read as records: table rows stay together with their column
names, headings are detected from font size, and prose blocks become
paragraphs in reading order.  A price list or a staff roster therefore keeps
each row's fields together instead of scattering cells across lines.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass

import pymupdf

from app.services.knowledge_records import (
    KnowledgeRecord,
    dedupe_records,
    make_record,
    render_records,
)

MAX_PDF_PAGES = 100
MAX_EXTRACTED_CHARS = 500_000
OCR_DPI = 200
MIN_PAGE_TEXT_CHARS = 20
MIN_DOCUMENT_TEXT_CHARS = 40
_HEADING_SIZE_RATIO = 1.18
_HEADING_MAX_WORDS = 14

_LANGUAGE_CODES = {
    "ar": "ara",
    "en": "eng",
    "hi": "hin",
    "ml": "mal",
}
_WHITESPACE = re.compile(r"[ \t\f\v]+")


class PdfIngestionError(ValueError):
    """A user-safe PDF validation or extraction failure."""


@dataclass(frozen=True)
class PreparedPdf:
    provider_content: bytes
    extracted_text: str
    extraction_method: str
    page_count: int
    sha256: str
    ocr_page_count: int
    records: tuple[KnowledgeRecord, ...] = ()


def _clean_page_text(value: str) -> str:
    lines = [_WHITESPACE.sub(" ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _has_usable_text(value: str) -> bool:
    meaningful = sum(character.isalnum() for character in value)
    return meaningful >= MIN_PAGE_TEXT_CHARS


def _ocr_language(languages: list[str] | None) -> str:
    requested = []
    for language in languages or ["en"]:
        code = _LANGUAGE_CODES.get(str(language).strip().lower().split("-", 1)[0])
        if code and code not in requested:
            requested.append(code)
    if "eng" not in requested:
        requested.insert(0, "eng")
    return "+".join(requested)


def _overlapping_table(
    block_bbox: tuple[float, float, float, float], tables: list[_PageTable]
) -> _PageTable | None:
    if not tables:
        return None
    rect = pymupdf.Rect(*block_bbox)
    for table in tables:
        intersection = rect & table.rect
        if not intersection.is_empty and intersection.get_area() >= rect.get_area() * 0.5:
            return table
    return None


@dataclass
class _PageTable:
    rect: pymupdf.Rect
    header_names: list[str]
    rows: list[list[str]]
    inserted: bool = False

    def records(self, *, page_number: int, heading_path: list[str]) -> list[KnowledgeRecord]:
        """Build this table's records under the heading that precedes it in layout."""
        records: list[KnowledgeRecord] = []
        if any(self.header_names):
            record = make_record(
                "heading",
                [" | ".join(name for name in self.header_names if name)],
                heading_path=heading_path,
                page=page_number,
            )
            if record:
                records.append(record)
        for values in self.rows:
            if self.header_names and len(self.header_names) == len(values):
                values = [
                    f"{name}: {value}" if name and value else value
                    for name, value in zip(self.header_names, values, strict=True)
                ]
            record = make_record("table_row", values, heading_path=heading_path, page=page_number)
            if record:
                records.append(record)
        return records


def _page_tables(page: pymupdf.Page) -> list[_PageTable]:
    """Detect the tables on a page, each kept with its own layout rectangle."""
    tables: list[_PageTable] = []
    try:
        detected = page.find_tables()
    except Exception:  # pragma: no cover - table detection is best effort
        return tables
    for table in detected:
        try:
            rows = table.extract()
        except Exception:  # pragma: no cover - a damaged table must not fail the page
            continue
        header_names = [
            " ".join(str(name or "").split()) for name in (getattr(table.header, "names", []) or [])
        ]
        external_header = bool(getattr(table.header, "external", False))
        if rows and not external_header and header_names and any(header_names):
            rows = rows[1:]
        cleaned_rows = []
        for row in rows:
            values = [" ".join(str(cell or "").split()) for cell in row]
            if any(values):
                cleaned_rows.append(values)
        if not cleaned_rows:
            continue
        tables.append(_PageTable(pymupdf.Rect(table.bbox), header_names, cleaned_rows))
    return tables


def _page_records(
    page: pymupdf.Page,
    *,
    page_number: int,
    heading_path: list[str],
    detect_tables: bool,
) -> list[KnowledgeRecord]:
    records: list[KnowledgeRecord] = []
    tables = _page_tables(page) if detect_tables else []
    try:
        layout = page.get_text("dict", sort=True)
    except Exception:  # pragma: no cover - fall back to plain text on odd pages
        layout = {"blocks": []}
    blocks: list[tuple[str, float, tuple[float, float, float, float]]] = []
    sizes: list[float] = []
    for block in layout.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        block_size = 0.0
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = " ".join(str(span.get("text") or "") for span in spans)
            text = _WHITESPACE.sub(" ", text).strip()
            if not text:
                continue
            lines.append(text)
            for span in spans:
                size = float(span.get("size") or 0.0)
                if size > 0 and str(span.get("text") or "").strip():
                    sizes.append(size)
                    block_size = max(block_size, size)
        if lines:
            blocks.append((" ".join(lines), block_size, tuple(block.get("bbox", (0, 0, 0, 0)))))
    if not blocks and not tables:
        text = _clean_page_text(page.get_text("text", sort=True))
        for paragraph in text.split("\n"):
            record = make_record(
                "paragraph", [paragraph], heading_path=heading_path, page=page_number
            )
            if record:
                records.append(record)
        return records
    body_size = statistics.median(sizes) if sizes else 0.0
    for text, size, bbox in blocks:
        table = _overlapping_table(bbox, tables)
        if table is not None:
            # Each table is placed once, where its cells sit in reading order,
            # under whichever heading precedes it on the page.
            if not table.inserted:
                records.extend(table.records(page_number=page_number, heading_path=heading_path))
                table.inserted = True
            continue
        words = text.split()
        if (
            body_size
            and size >= body_size * _HEADING_SIZE_RATIO
            and len(words) <= _HEADING_MAX_WORDS
        ):
            heading_path[:] = [text]
            record = make_record("heading", [text], page=page_number)
        else:
            record = make_record("paragraph", [text], heading_path=heading_path, page=page_number)
        if record:
            records.append(record)
    for table in tables:
        if not table.inserted:
            records.extend(table.records(page_number=page_number, heading_path=heading_path))
    return records


def prepare_pdf(content: bytes, *, languages: list[str] | None = None) -> PreparedPdf:
    """Validate, extract records, and make scanned pages searchable."""
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise PdfIngestionError("The PDF is damaged or cannot be opened.") from exc

    try:
        if document.needs_pass:
            raise PdfIngestionError("Password-protected PDFs are not supported.")
        if document.page_count < 1:
            raise PdfIngestionError("The PDF contains no pages.")
        if document.page_count > MAX_PDF_PAGES:
            raise PdfIngestionError(f"PDFs may contain at most {MAX_PDF_PAGES} pages.")

        page_records: list[list[KnowledgeRecord]] = []
        pages_needing_ocr: list[int] = []
        heading_path: list[str] = []
        for page_number in range(document.page_count):
            page = document[page_number]
            text = _clean_page_text(page.get_text("text", sort=True))
            if not _has_usable_text(text):
                pages_needing_ocr.append(page_number)
                page_records.append([])
                continue
            page_records.append(
                _page_records(
                    page,
                    page_number=page_number + 1,
                    heading_path=heading_path,
                    detect_tables=True,
                )
            )

        provider_content = content
        if pages_needing_ocr:
            searchable_document = pymupdf.open()
            ocr_language = _ocr_language(languages)
            try:
                for page_number in range(document.page_count):
                    if page_number not in pages_needing_ocr:
                        searchable_document.insert_pdf(
                            document,
                            from_page=page_number,
                            to_page=page_number,
                        )
                        continue
                    page = document[page_number]
                    pixmap = page.get_pixmap(
                        dpi=OCR_DPI,
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    )
                    ocr_document = pymupdf.open(
                        "pdf",
                        pixmap.pdfocr_tobytes(language=ocr_language),
                    )
                    try:
                        page_records[page_number] = _page_records(
                            ocr_document[0],
                            page_number=page_number + 1,
                            heading_path=heading_path,
                            detect_tables=False,
                        )
                        searchable_document.insert_pdf(ocr_document)
                    finally:
                        ocr_document.close()
                provider_content = searchable_document.tobytes(garbage=4, deflate=True)
            except Exception as exc:
                raise PdfIngestionError(
                    "This PDF contains scanned pages, but OCR could not read them. "
                    "Try a clearer scan or a text-searchable PDF."
                ) from exc
            finally:
                searchable_document.close()

        records = dedupe_records(record for page_items in page_records for record in page_items)
        bounded: list[KnowledgeRecord] = []
        total = 0
        for record in records:
            total += len(record.text) + 2
            if total > MAX_EXTRACTED_CHARS:
                break
            bounded.append(record)
        extracted_text = render_records(bounded)
        if sum(character.isalnum() for character in extracted_text) < MIN_DOCUMENT_TEXT_CHARS:
            raise PdfIngestionError(
                "VAV could not find enough readable text in this PDF, even after OCR."
            )

        method = "native"
        if pages_needing_ocr:
            method = "ocr" if len(pages_needing_ocr) == document.page_count else "hybrid"
        return PreparedPdf(
            provider_content=provider_content,
            extracted_text=extracted_text,
            extraction_method=method,
            page_count=document.page_count,
            sha256=hashlib.sha256(content).hexdigest(),
            ocr_page_count=len(pages_needing_ocr),
            records=tuple(bounded),
        )
    finally:
        document.close()
