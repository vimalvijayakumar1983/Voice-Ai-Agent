"""URL/PDF/text share compilation; original and live snapshots stay authoritative."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.api.v1.endpoints import knowledge as endpoint
from app.models.agent import KnowledgeBase, KnowledgeSource
from app.services import knowledge_compiler as compiler
from app.services.pdf_ingestion import PreparedPdf

RAW = "Example Clinic offers PRP consultations for AED 300 after a doctor assessment."
FACT = {
    "subject": "Example Clinic",
    "predicate": "consultation price",
    "value": "AED 300",
    "evidence": RAW,
    "search_phrases": ["What does a PRP consultation cost?"],
}


class FakeAI:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "page_type": "service",
                                "entities": [
                                    {
                                        "name": "Example Clinic",
                                        "entity_type": "organization",
                                        "evidence": RAW,
                                    }
                                ],
                                "facts": [
                                    FACT,
                                    {**FACT, "value": "AED 999", "evidence": "Invented price"},
                                ],
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


@pytest.fixture
def fake_ai(monkeypatch):
    ai = FakeAI()

    async def compile_with_fake(**kwargs):
        return await compiler.compile_source_knowledge(
            **{**kwargs, "api_key": "fake", "client": ai}
        )

    monkeypatch.setattr(endpoint, "compile_source_knowledge", compile_with_fake)
    return ai


async def create_kb(db, tenant):
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name="Unified test",
        provider="smallest",
        provider_knowledge_base_id="remote-test",
        sync_status="ready",
    )
    db.add(kb)
    await db.commit()
    return kb


async def get_source(db, source_id):
    return await db.scalar(
        select(KnowledgeSource)
        .where(KnowledgeSource.id == UUID(source_id))
        .execution_options(populate_existing=True)
    )


@pytest.mark.asyncio
async def test_text_uses_grounding_compiler_and_preserves_original(
    client, auth_headers, tenant, db, fake_ai
):
    kb = await create_kb(db, tenant)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "Clinic FAQ", "content": RAW},
    )
    assert response.status_code == 200, response.text
    source = await get_source(db, response.json()["sources"][0]["id"])
    assert source.raw_content == RAW
    assert source.content.endswith(RAW)
    assert source.content_sha256 == hashlib.sha256(RAW.encode()).hexdigest()
    assert source.structured_content["facts"] == [FACT]
    assert source.source_metadata["compiler"]["warning"]
    assert source.compiled_at is not None
    assert response.json()["approval_status"] == "draft"
    assert len(fake_ai.requests) == 1  # short FAQs also need company attribution
    assert "AED 999" not in source.content


@pytest.mark.asyncio
async def test_pdf_uses_same_facts_and_keeps_binary_original(
    client, auth_headers, tenant, db, fake_ai, monkeypatch
):
    items = []

    class Provider:
        async def list_knowledge_items(self, _):
            return list(items)

        async def upload_knowledge_pdf(self, **kwargs):
            assert kwargs["content"] == b"%PDF-searchable"
            items.append(
                {"_id": "pdf-1", "fileName": kwargs["file_name"], "processingStatus": "processing"}
            )
            return {"data": {"_id": "pdf-1"}}

    monkeypatch.setattr(endpoint, "get_smallest_client", Provider)
    monkeypatch.setattr(
        endpoint,
        "prepare_pdf",
        lambda *args, **kwargs: PreparedPdf(
            provider_content=b"%PDF-searchable",
            extracted_text=RAW,
            extraction_method="ocr",
            page_count=1,
            sha256="a" * 64,
            ocr_page_count=1,
        ),
    )
    kb = await create_kb(db, tenant)
    original = b"%PDF-1.4 original scanned bytes"
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("clinic.pdf", original, "application/pdf")},
        data={"processing_mode": "ai_verified"},
    )
    assert response.status_code == 200, response.text
    source = await get_source(db, response.json()["sources"][0]["id"])
    assert source.file_content == original
    assert source.raw_content == RAW
    assert source.structured_content["facts"] == [FACT]
    assert source.source_metadata["ocr_page_count"] == 1
    assert source.source_metadata["compiler"]["requested_mode"] == "ai_verified"
    assert source.status == "processing"  # local compilation != remote indexing
    assert len(fake_ai.requests) == 1


@pytest.mark.asyncio
async def test_existing_text_recompile_stages_in_place_and_keeps_live_snapshot(
    client, auth_headers, tenant, db, fake_ai
):
    kb = await create_kb(db, tenant)
    source = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        source_type="text",
        name="Legacy FAQ",
        content=RAW,
        status="indexed",
    )
    db.add(source)
    await db.commit()
    approved = await client.post(
        f"/api/v1/knowledge/{kb.id}/approval", headers=auth_headers, json={"approved": True}
    )
    assert approved.status_code == 200, approved.text
    revision = approved.json()["serving_revision"]
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/{source.id}/compile",
        headers=auth_headers,
        json={"processing_mode": "ai_verified"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_count"] == 1
    assert body["sources"][0]["id"] == str(source.id)
    assert body["serving_revision"] == revision
    assert body["has_pending_changes"] is True
    assert body["approval_status"] == "draft"
    persisted = await get_source(db, str(source.id))
    assert persisted.structured_content["facts"] == [FACT]
    assert persisted.raw_content == RAW


@pytest.mark.asyncio
async def test_ai_failure_leaves_approved_source_untouched(
    client, auth_headers, tenant, db, monkeypatch
):
    kb = await create_kb(db, tenant)
    source = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        source_type="text",
        name="Legacy",
        content=RAW,
        status="indexed",
    )
    db.add(source)
    await db.commit()
    approved = await client.post(
        f"/api/v1/knowledge/{kb.id}/approval", headers=auth_headers, json={"approved": True}
    )

    async def fail(**kwargs):
        raise compiler.KnowledgeCompilerError("AI unavailable; retry later.")

    monkeypatch.setattr(endpoint, "compile_source_knowledge", fail)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/{source.id}/compile",
        headers=auth_headers,
        json={"processing_mode": "ai_verified"},
    )
    assert response.status_code == 422
    current = await client.get(f"/api/v1/knowledge/{kb.id}", headers=auth_headers)
    assert current.json()["approval_status"] == "approved"
    assert current.json()["serving_revision"] == approved.json()["serving_revision"]
    assert (await get_source(db, str(source.id))).content == RAW


@pytest.mark.asyncio
async def test_recompile_scopes_source_to_knowledge_base(client, auth_headers, tenant, db, fake_ai):
    kb = await create_kb(db, tenant)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/{uuid4()}/compile", headers=auth_headers, json={}
    )
    assert response.status_code == 404
    assert fake_ai.requests == []


@pytest.mark.asyncio
async def test_long_source_does_not_truncate_tail(monkeypatch):
    visited = []

    async def segment(**kwargs):
        text = kwargs["text"]
        visited.append(text)
        structured = compiler._deterministic_structure(title="Long PDF", url="", text=text)
        structured["facts"] = [FACT] if RAW in text else []
        return structured, 100, 20

    monkeypatch.setattr(compiler, "_compile_ai", segment)
    text = "Header\n" + "Approved policy text. " * 12_000 + "\n" + RAW
    result = await compiler.compile_source_knowledge(
        title="Long PDF", url="", text=text, requested_mode="ai_verified", api_key="fake"
    )
    assert len(visited) == 3
    assert all(len(part) <= 120_000 for part in visited)
    assert RAW in visited[-1]
    assert result.content.endswith(text)
    assert result.structured["facts"] == [FACT]
    assert result.input_tokens == 300
    assert result.structured["exact_fact_coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_automatic_fallback_is_explicit_and_preserves_text():
    result = await compiler.compile_source_knowledge(
        title="FAQ", url="", text=RAW, requested_mode="automatic", require_structured_facts=True
    )
    assert result.warning
    assert result.effective_mode == "fast"
    assert result.content.endswith(RAW)
    assert result.structured["facts"] == []


@pytest.mark.asyncio
async def test_fast_recompile_is_idempotent(client, auth_headers, tenant, db):
    kb = await create_kb(db, tenant)
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW, "processing_mode": "fast"},
    )
    source_id = added.json()["sources"][0]["id"]
    before = (await get_source(db, source_id)).compiled_at
    for _ in range(2):
        response = await client.post(
            f"/api/v1/knowledge/{kb.id}/sources/{source_id}/compile",
            headers=auth_headers,
            json={"processing_mode": "fast"},
        )
        assert response.status_code == 200
        assert response.json()["source_count"] == 1
    assert (await get_source(db, source_id)).compiled_at == before


@pytest.mark.asyncio
async def test_identical_text_retry_does_not_duplicate_or_invalidate_approval(
    client,
    auth_headers,
    tenant,
    db,
):
    kb = await create_kb(db, tenant)
    payload = {"name": "FAQ", "content": RAW, "processing_mode": "fast"}
    first = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text", headers=auth_headers, json=payload
    )
    approved = await client.post(
        f"/api/v1/knowledge/{kb.id}/approval", headers=auth_headers, json={"approved": True}
    )
    second = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text", headers=auth_headers, json=payload
    )
    assert second.status_code == 200
    assert second.json()["source_count"] == 1
    assert second.json()["sources"][0]["id"] == first.json()["sources"][0]["id"]
    assert second.json()["approval_status"] == "approved"
    assert second.json()["serving_revision"] == approved.json()["serving_revision"]


@pytest.mark.asyncio
async def test_preview_requires_auth_and_checks_source_ownership(
    client, auth_headers, tenant, db, fake_ai
):
    kb = await create_kb(db, tenant)
    other = await create_kb(db, tenant)
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW},
    )
    source_id = added.json()["sources"][0]["id"]
    preview_url = f"/api/v1/knowledge/{kb.id}/sources/{source_id}/preview"
    response = await client.get(preview_url, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["raw_text"] == RAW
    assert response.json()["structured_content"]["facts"] == [FACT]
    assert (await client.get(preview_url)).status_code in {401, 403}
    for action in ("preview", "compile"):
        url = f"/api/v1/knowledge/{other.id}/sources/{source_id}/{action}"
        response = (
            await client.get(url, headers=auth_headers)
            if action == "preview"
            else (await client.post(url, headers=auth_headers, json={}))
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_recompile_cannot_overwrite_concurrent_source_edit(
    client, auth_headers, tenant, db, monkeypatch
):
    kb = await create_kb(db, tenant)
    source = KnowledgeSource(
        tenant_id=tenant.id,
        knowledge_base_id=kb.id,
        source_type="text",
        name="Legacy",
        content=RAW,
        status="indexed",
    )
    db.add(source)
    await db.commit()
    source_id = source.id

    async def concurrent_edit(**kwargs):
        await db.execute(
            update(KnowledgeSource)
            .where(KnowledgeSource.id == source_id)
            .values(
                content="A newer user edit that must be preserved.",
                updated_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        await db.commit()
        return await compiler.compile_source_knowledge(**{**kwargs, "requested_mode": "fast"})

    monkeypatch.setattr(endpoint, "compile_source_knowledge", concurrent_edit)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/{source_id}/compile", headers=auth_headers, json={}
    )
    assert response.status_code == 409
    assert (
        await get_source(db, str(source_id))
    ).content == "A newer user edit that must be preserved."


@pytest.mark.asyncio
async def test_pdf_compiler_failure_precedes_remote_upload(
    client, auth_headers, tenant, db, monkeypatch
):
    kb = await create_kb(db, tenant)
    monkeypatch.setattr(
        endpoint,
        "prepare_pdf",
        lambda *args, **kwargs: PreparedPdf(
            provider_content=b"%PDF-searchable",
            extracted_text=RAW,
            extraction_method="native",
            page_count=1,
            sha256="a" * 64,
            ocr_page_count=0,
        ),
    )

    async def fail(**kwargs):
        raise compiler.KnowledgeCompilerError("Compilation failed; original draft unchanged.")

    def unexpected_provider():
        raise AssertionError("No remote upload is permitted after compilation failure")

    monkeypatch.setattr(endpoint, "compile_source_knowledge", fail)
    monkeypatch.setattr(endpoint, "get_smallest_client", unexpected_provider)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("clinic.pdf", b"%PDF-original", "application/pdf")},
        data={"processing_mode": "ai_verified"},
    )
    assert response.status_code == 422
    current = await client.get(f"/api/v1/knowledge/{kb.id}", headers=auth_headers)
    assert current.json()["source_count"] == 0
