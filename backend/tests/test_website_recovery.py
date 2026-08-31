import pymupdf
import pytest

from app.services import website_recovery
from app.services.website_recovery import (
    RecoveredPage,
    WebsiteRecoveryError,
    download_html,
    extract_readable_text,
    recover_page,
    recovery_metadata,
    searchable_pdf,
)
from app.tasks.knowledge_tasks import _wait_for_provider_index


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


@pytest.mark.asyncio
async def test_provider_verification_waits_for_indexed_item(monkeypatch):
    class Provider:
        def __init__(self):
            self.calls = 0

        async def list_knowledge_items(self, _knowledge_base_id):
            self.calls += 1
            state = "processing" if self.calls == 1 else "completed"
            return [
                {
                    "_id": "provider-item-1",
                    "fileName": "recovered.pdf",
                    "processingStatus": state,
                }
            ]

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.tasks.knowledge_tasks._provider_poll_wait", no_wait)
    provider = Provider()

    await _wait_for_provider_index(
        provider,
        knowledge_base_id="provider-kb-1",
        provider_item_id="provider-item-1",
        artifact_name="recovered.pdf",
    )

    assert provider.calls == 2
