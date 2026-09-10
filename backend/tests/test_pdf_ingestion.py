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


def _fee_schedule_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 60), "Royal Medical Fee Schedule", fontsize=18)
    page.insert_text((72, 90), "All prices include VAT.", fontsize=11)
    rows = [
        ["Service", "Price", "Duration"],
        ["Consultation", "AED 150", "20 min"],
        ["Follow-up visit", "AED 100", "15 min"],
        ["Dental cleaning", "AED 250", "45 min"],
    ]
    x0, y0, width, height = 72, 120, 150, 22
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            rect = pymupdf.Rect(
                x0 + column_index * width,
                y0 + row_index * height,
                x0 + (column_index + 1) * width,
                y0 + (row_index + 1) * height,
            )
            page.draw_rect(rect, color=(0, 0, 0), width=0.5)
            page.insert_text((rect.x0 + 4, rect.y0 + 15), cell, fontsize=10)
    page.insert_text((72, 260), "Cancellations require 24 hours notice.", fontsize=11)
    content = document.tobytes()
    document.close()
    return content


def test_prepare_pdf_keeps_each_price_row_together_with_column_names():
    prepared = prepare_pdf(_fee_schedule_pdf(), languages=["en"])

    rows = [record for record in prepared.records if record.kind == "table_row"]
    assert [record.text for record in rows] == [
        "Service: Consultation | Price: AED 150 | Duration: 20 min",
        "Service: Follow-up visit | Price: AED 100 | Duration: 15 min",
        "Service: Dental cleaning | Price: AED 250 | Duration: 45 min",
    ]
    assert all(record.page == 1 for record in rows)
    headings = [record.text for record in prepared.records if record.kind == "heading"]
    assert "Royal Medical Fee Schedule" in headings
    paragraphs = [record.text for record in prepared.records if record.kind == "paragraph"]
    assert "All prices include VAT." in paragraphs
    assert "Cancellations require 24 hours notice." in paragraphs
    assert "Service: Consultation | Price: AED 150 | Duration: 20 min" in prepared.extracted_text
