import pymupdf
import pytest

from app.services import website_recovery
from app.services.website_recovery import (
    RecoveredPage,
    WebsiteRecoveryError,
    _is_related_site_hostname,
    download_html,
    extract_page_records,
    extract_readable_text,
    find_next_page_url,
    recover_page,
    recovery_metadata,
    searchable_pdf,
)


def test_extract_readable_text_removes_scripts_and_keeps_structured_content():
    title, text = extract_readable_text(
        """
        <html><head><title>Clinic services</title>
        <meta name="description" content="Approved medical centre information">
        <script>alert('not knowledge')</script>
        <script type="application/ld+json">
          {"@type":"MedicalClinic","telephone":"+971 2 555 0100"}
        </script></head>
        <body><nav>Repeated navigation</nav><main>
          <h1>Dermatology</h1>
          <p>Appointments are available every day from 9 AM to 9 PM.</p>
        </main></body></html>
        """,
        url="https://clinic.example/services",
    )

    assert title == "Clinic services"
    assert "Dermatology" in text
    assert "Appointments are available" in text
    assert "+971 2 555 0100" in text
    assert "alert" not in text
    assert "Repeated navigation" not in text


def test_extract_readable_text_removes_repeated_page_chrome():
    _title, text = extract_readable_text(
        """
        <html><head><title>Chemical peeling</title></head><body>
          <header><p>Clinic phone repeated on every page</p></header>
          <nav><p>Home Treatments Doctors Offers</p></nav>
          <main><h1>Chemical peeling</h1>
            <p>Chemical peeling guidance that is long enough to be useful to callers,
            including preparation, consultation and aftercare information.</p>
          </main>
          <footer><p>Copyright and repeated footer links</p></footer>
        </body></html>
        """,
        url="https://clinic.example/treatments/peeling",
    )

    assert "preparation, consultation and aftercare" in text
    assert "phone repeated" not in text
    assert "footer links" not in text


def test_related_api_host_scope_accepts_only_same_site_subdomains():
    assert _is_related_site_hostname("aecmc.com", "adminxpanel.aecmc.com")
    assert _is_related_site_hostname("www.clinic.example", "api.clinic.example")
    assert not _is_related_site_hostname("clinic.example", "clinic.example.attacker.test")
    assert not _is_related_site_hostname("clinic.example", "unrelated.example")


@pytest.mark.asyncio
async def test_recover_page_uses_javascript_fallback_when_static_page_is_empty(monkeypatch):
    async def static_page(_url):
        return (
            "https://clinic.example/doctors",
            "<html><body><div id='app'></div></body></html>",
            49,
        )

    async def rendered_page(_url):
        return (
            "<html><head><title>Doctors</title></head><body><main>"
            "<h1>Our doctors</h1><p>Dr Example provides dermatology consultations "
            "and cosmetic treatment guidance throughout the week.</p>"
            "</main></body></html>",
            240,
        )

    monkeypatch.setattr(website_recovery, "download_html", static_page)
    monkeypatch.setattr(website_recovery, "render_html", rendered_page)

    recovered = await recover_page("https://clinic.example/doctors")

    assert recovered == RecoveredPage(
        "https://clinic.example/doctors",
        "Doctors",
        recovered.text,
        "javascript_render",
        240,
    )
    assert "Dr Example" in recovered.text


def test_searchable_pdf_contains_recovered_page_text():
    value = searchable_pdf(
        title="Clinic services",
        url="https://clinic.example/services",
        text="Botox and PRP consultations are available after clinical assessment.",
    )

    assert value.startswith(b"%PDF-")
    document = pymupdf.open(stream=value, filetype="pdf")
    extracted = "\n".join(page.get_text() for page in document)
    assert "Botox and PRP consultations" in extracted
    assert "clinic.example/services" in extracted


def test_recovery_metadata_preserves_attempt_count_and_exposes_progress():
    value = recovery_metadata(
        {"recovery_attempts": 2},
        stage="rendering",
        message="Rendering JavaScript",
    )

    assert value["recovery_attempts"] == 2
    assert value["recovery"]["status"] == "processing"
    assert value["recovery"]["stage"] == "rendering"
    assert value["recovery"]["message"] == "Rendering JavaScript"


@pytest.mark.asyncio
async def test_download_rejects_redirect_to_private_network(monkeypatch):
    async def redirect(_url):
        return 302, {"location": "https://127.0.0.1/internal"}, b""

    monkeypatch.setattr(website_recovery, "_download_once", redirect)

    with pytest.raises(WebsiteRecoveryError, match="public") as error:
        await download_html("https://clinic.example")

    assert error.value.code == "unsafe_url"


_DIRECTORY_HTML = """
<html><head><title>Our Doctors - Royal Medical</title></head><body>
<nav><a href="/">Home</a><a href="/offers">Offers</a></nav>
<main>
  <h1>Our Doctors</h1>
  <div class="grid">
    <div class="card"><h3>Dr Randa Ahmed</h3><p>General Practitioner</p>
      <p>15+ Years Experience</p><a href="/doctors/randa">View Profile</a></div>
    <div class="card"><h3>Dr Loubna Saleh</h3><p>Pediatrics Specialist</p>
      <p>12+ Years Experience</p><a href="/doctors/loubna">View Profile</a></div>
    <div class="card"><h3>Dr Dalia Hassan</h3><p>General Practitioner</p>
      <p>23+ Years Experience</p><a href="/doctors/dalia">View Profile</a></div>
    <div class="card"><h3>Dr Rana Youssef</h3><p>General Practitioner</p>
      <p>15+ Years Experience</p><a href="/doctors/rana">View Profile</a></div>
  </div>
  <h2>Fees</h2>
  <table>
    <thead><tr><th>Service</th><th>Price</th></tr></thead>
    <tbody><tr><td>Consultation</td><td>AED 150</td></tr>
    <tr><td>Follow-up</td><td>AED 100</td></tr></tbody>
  </table>
  <p>Royal Medical Center provides comprehensive family care in Abu Dhabi for every
  member of the family, every day of the week, with walk-in and booked visits.</p>
</main></body></html>
"""


def test_extract_page_records_keeps_each_directory_card_whole():
    title, records = extract_page_records(_DIRECTORY_HTML, url="https://clinic.example/doctors")

    assert title == "Our Doctors - Royal Medical"
    cards = [record for record in records if record.kind == "card"]
    assert [record.text for record in cards] == [
        "Dr Randa Ahmed | General Practitioner | 15+ Years Experience",
        "Dr Loubna Saleh | Pediatrics Specialist | 12+ Years Experience",
        "Dr Dalia Hassan | General Practitioner | 23+ Years Experience",
        "Dr Rana Youssef | General Practitioner | 15+ Years Experience",
    ]
    assert all(record.heading_path == ("Our Doctors",) for record in cards)
    # A label shared by several cards must survive on every card.
    assert sum("General Practitioner" in record.text for record in cards) == 3
    assert not any("View Profile" in record.text for record in records)
    assert "Home" not in extract_readable_text(_DIRECTORY_HTML, url="u")[1]


def test_extract_page_records_keeps_table_rows_with_column_names():
    _title, records = extract_page_records(_DIRECTORY_HTML, url="https://clinic.example/doctors")

    rows = [record for record in records if record.kind == "table_row"]
    assert [record.text for record in rows] == [
        "Service: Consultation | Price: AED 150",
        "Service: Follow-up | Price: AED 100",
    ]
    assert rows[0].heading_path == ("Our Doctors", "Fees")
    assert any(record.kind == "paragraph" and "family care" in record.text for record in records)


def test_readable_text_renders_one_record_per_paragraph():
    _title, text = extract_readable_text(_DIRECTORY_HTML, url="https://clinic.example/doctors")

    paragraphs = text.split("\n\n")
    assert "Dr Dalia Hassan | General Practitioner | 23+ Years Experience" in paragraphs
    assert paragraphs.count("General Practitioner") == 0


_PAGINATED_DIRECTORY_HTML = """
<html><head><title>Our Doctors - Royal Medical</title>
<link rel="next" href="/doctors?page=2"></head><body>
<main>
<form class="filters" role="search">
  <label>Specialty</label>
  <select><option>All Specialties</option><option>Cardiology</option></select>
  <label>Gender</label><select><option>Any</option></select>
  <button type="submit">Search</button>
</form>
<div class="filters"><label>Gender</label><select><option>Any</option></select></div>
<p>Our doctors provide family medicine, paediatrics and dental care across Abu Dhabi
with same-day appointments and insurance support for every patient.</p>
<div class="card"><h3>Dr Randa Ahmed</h3><p>General Practitioner</p>
<p>22+ Years Experience</p></div>
<div class="card"><h3>Dr Dalia Wahba</h3><p>Anesthesiologist</p>
<p>23+ Years Experience</p></div>
<div class="pagination"><a href="/doctors?page=1">1</a><a href="/doctors?page=2">2</a>
<a href="/doctors?page=2">Next</a></div>
</main></body></html>
"""


def test_filter_controls_and_pagination_widgets_are_not_records():
    _title, records = extract_page_records(
        _PAGINATED_DIRECTORY_HTML, url="https://royalmedical.ae/doctors"
    )

    texts = [record.text for record in records]
    assert "Dr Randa Ahmed | General Practitioner | 22+ Years Experience" in texts
    assert "Dr Dalia Wahba | Anesthesiologist | 23+ Years Experience" in texts
    assert not any(
        "All Specialties" in text or text in {"Any", "Next", "Search", "Gender", "Specialty"}
        for text in texts
    )
    assert not any(text.startswith("1 | 2") for text in texts)


def test_find_next_page_url_prefers_rel_next_and_stays_on_site():
    assert (
        find_next_page_url(_PAGINATED_DIRECTORY_HTML, url="https://royalmedical.ae/doctors")
        == "https://royalmedical.ae/doctors?page=2"
    )
    by_text = '<a href="/doctors?page=3">Next</a>'
    assert (
        find_next_page_url(by_text, url="https://royalmedical.ae/doctors?page=2")
        == "https://royalmedical.ae/doctors?page=3"
    )
    assert find_next_page_url(by_text, url="https://royalmedical.ae/doctors?page=3") is None
    off_site = '<a rel="next" href="https://other.example/doctors?page=2">Next</a>'
    assert find_next_page_url(off_site, url="https://royalmedical.ae/doctors") is None
    assert find_next_page_url("<p>No links</p>", url="https://royalmedical.ae/doctors") is None
