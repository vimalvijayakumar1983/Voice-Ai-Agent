import pymupdf
import pytest

from app.services.pdf_ingestion import PdfIngestionError, prepare_pdf


def _text_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


def test_prepare_pdf_extracts_searchable_text_without_rewriting_native_pdf():
    content = _text_pdf("Botox and PRP treatment guidance for customer support agents.")

    prepared = prepare_pdf(content, languages=["en"])

    assert prepared.extraction_method == "native"
    assert prepared.ocr_page_count == 0
    assert prepared.page_count == 1
    assert "Botox and PRP" in prepared.extracted_text
    assert prepared.provider_content == content


@pytest.mark.parametrize("content", [b"not a pdf", b"%PDF-1.4\nbroken"])
def test_prepare_pdf_rejects_unreadable_documents(content):
    with pytest.raises(PdfIngestionError):
        prepare_pdf(content)
