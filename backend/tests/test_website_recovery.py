import pymupdf
import pytest

from app.services import website_recovery
from app.services.website_recovery import (
    RecoveredPage,
    WebsiteRecoveryError,
    _is_related_site_hostname,
    download_html,
    extract_readable_text,
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
