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


def _two_branch_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 60), "Opening hours", fontsize=18)
    tables = (
        (90, 120, "Branch A", [["Day", "Hours"], ["Monday", "9 AM to 5 PM"], ["Friday", "Closed"]]),
        (
            250,
            280,
            "Branch B",
            [["Day", "Hours"], ["Monday", "9 AM to 5 PM"], ["Friday", "2 PM to 8 PM"]],
        ),
    )
    for heading_y, table_y, branch, rows in tables:
        page.insert_text((72, heading_y), branch, fontsize=14)
        for row_index, row in enumerate(rows):
            for column_index, cell in enumerate(row):
                rect = pymupdf.Rect(
                    72 + column_index * 150,
                    table_y + row_index * 22,
                    72 + (column_index + 1) * 150,
                    table_y + (row_index + 1) * 22,
                )
                page.draw_rect(rect, color=(0, 0, 0), width=0.5)
                page.insert_text((rect.x0 + 4, rect.y0 + 15), cell, fontsize=10)
    page.insert_text((72, 215), "Branch A closes on public holidays.", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def test_prepare_pdf_places_each_table_under_its_own_heading():
    prepared = prepare_pdf(_two_branch_pdf(), languages=["en"])

    sequence = [(record.kind, record.text) for record in prepared.records]
    assert sequence.index(("heading", "Branch A")) < sequence.index(
        ("table_row", "Day: Monday | Hours: 9 AM to 5 PM")
    )
    assert sequence.index(("paragraph", "Branch A closes on public holidays.")) < sequence.index(
        ("heading", "Branch B")
    )
    assert sequence.index(("heading", "Branch B")) < sequence.index(
        ("table_row", "Day: Friday | Hours: 2 PM to 8 PM")
    )
    monday_rows = [
        record for record in prepared.records if record.text == "Day: Monday | Hours: 9 AM to 5 PM"
    ]
    assert [record.heading_path for record in monday_rows] == [("Branch A",), ("Branch B",)]
