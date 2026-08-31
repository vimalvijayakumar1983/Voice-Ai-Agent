"""Safe, retrieval-ready PDF ingestion with selective OCR fallback."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import pymupdf

MAX_PDF_PAGES = 100
MAX_EXTRACTED_CHARS = 500_000
OCR_DPI = 200
MIN_PAGE_TEXT_CHARS = 20
MIN_DOCUMENT_TEXT_CHARS = 40

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


def prepare_pdf(content: bytes, *, languages: list[str] | None = None) -> PreparedPdf:
    """Validate, extract, and make scanned pages searchable before provider upload."""
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

        page_text: list[str] = []
        pages_needing_ocr: list[int] = []
        for page_number in range(document.page_count):
            text = _clean_page_text(document[page_number].get_text("text", sort=True))
            page_text.append(text)
            if not _has_usable_text(text):
                pages_needing_ocr.append(page_number)

        provider_content = content
        if pages_needing_ocr:
            provider_document = pymupdf.open()
            ocr_language = _ocr_language(languages)
            try:
                for page_number in range(document.page_count):
                    if page_number not in pages_needing_ocr:
                        provider_document.insert_pdf(
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
                        page_text[page_number] = _clean_page_text(
                            ocr_document[0].get_text("text", sort=True)
                        )
                        provider_document.insert_pdf(ocr_document)
                    finally:
                        ocr_document.close()
                provider_content = provider_document.tobytes(garbage=4, deflate=True)
            except Exception as exc:
                raise PdfIngestionError(
                    "This PDF contains scanned pages, but OCR could not read them. "
                    "Try a clearer scan or a text-searchable PDF."
                ) from exc
            finally:
                provider_document.close()

        extracted_text = "\n\n".join(text for text in page_text if text).strip()
        extracted_text = extracted_text[:MAX_EXTRACTED_CHARS]
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
        )
    finally:
        document.close()
