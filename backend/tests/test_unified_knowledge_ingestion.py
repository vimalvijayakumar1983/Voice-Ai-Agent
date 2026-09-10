"""URL/PDF/text share compilation; original and live snapshots stay authoritative."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.endpoints import knowledge as endpoint
from app.models.agent import KnowledgeBase, KnowledgeSource
from app.services import knowledge_compiler as compiler
from app.services.pdf_ingestion import PreparedPdf
from app.tasks import knowledge_compile_tasks as jobs


@pytest.fixture(autouse=True)
def background_sessions(db, monkeypatch):
    monkeypatch.setattr(
        jobs, "async_session_factory", async_sessionmaker(db.bind, expire_on_commit=False)
    )


async def finish_upload(body, db):
    source = body["sources"][0]
    job = source["source_metadata"]["upload_compile"]
    tenant_id = await db.scalar(
        select(KnowledgeBase.tenant_id).where(KnowledgeBase.id == UUID(body["id"]))
    )
    await db.commit()
    await jobs._compile(str(tenant_id), body["id"], source["id"], job["run_id"])


RAW = "Example Clinic offers PRP consultations for AED 300 after a doctor assessment."
FACT = {
    "subject": "Example Clinic",
    "predicate": "consultation price",
    "value": "AED 300",
    "evidence": RAW,
    "search_phrases": ["What does a PRP consultation cost?"],
}


@pytest.mark.asyncio
async def test_background_inference_has_one_longer_attempt(
    client, auth_headers, tenant, db, fake_ai, monkeypatch
):
    kb = await create_kb(db, tenant)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "Clinic FAQ", "content": RAW, "processing_mode": "ai_verified"},
    )
    assert response.status_code == 200
    captured = {}
    delegate = jobs.compile_source_knowledge

    async def capture(**kwargs):
        captured.update(kwargs)
        return await delegate(**kwargs)

    monkeypatch.setattr(jobs, "compile_source_knowledge", capture)
    await finish_upload(response.json(), db)
    assert captured["timeout_seconds"] == 120.0
    assert captured["max_retries"] == 0
    assert len(fake_ai.requests) == 1
    source = await get_source(db, response.json()["sources"][0]["id"])
    assert jobs.job_for(source)["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds,max_retries", [(45.0, 1), (120.0, 0)])
async def test_compiler_passes_request_budget_to_owned_client(
    monkeypatch, timeout_seconds, max_retries
):
    captured = {}
    ai = FakeAI()

    async def close():
        captured["closed"] = True

    ai.close = close

    def factory(**kwargs):
        captured.update(kwargs)
        return ai

    monkeypatch.setattr(compiler, "AsyncOpenAI", factory)
    options = (
        {}
        if timeout_seconds == 45.0
        else {"timeout_seconds": timeout_seconds, "max_retries": max_retries}
    )
    result = await compiler.compile_source_knowledge(
        title="Clinic FAQ",
        url="",
        text=RAW,
        requested_mode="ai_verified",
        api_key="fake",
        **options,
    )
    assert captured["timeout"] == timeout_seconds
    assert captured["max_retries"] == max_retries
    assert captured["closed"]
    assert result.structured["facts"] == [FACT]


@pytest.mark.asyncio
async def test_background_timeout_is_not_reported_as_bad_credentials(
    client, auth_headers, tenant, db, fake_ai, monkeypatch
):
    kb = await create_kb(db, tenant)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "Clinic FAQ", "content": RAW, "processing_mode": "ai_verified"},
    )
    assert response.status_code == 200

    async def timed_out(**kwargs):
        try:
            raise TimeoutError("secret provider response")
        except TimeoutError as exc:
            raise compiler.KnowledgeCompilerError("generic wrapper") from exc

    monkeypatch.setattr(jobs, "compile_source_knowledge", timed_out)
    await finish_upload(response.json(), db)
    source = await get_source(db, response.json()["sources"][0]["id"])
    assert source.status == "failed"
    assert source.raw_content == RAW
    assert "timed out" in source.error_message
    assert "connection" not in source.error_message
    assert "secret" not in source.error_message
    assert jobs.job_for(source)["status"] == "failed"


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
    monkeypatch.setattr(jobs, "compile_source_knowledge", compile_with_fake)
    return ai


async def create_kb(db, tenant):
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name="Unified test",
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
    assert response.json()["sync_status"] == "processing"
    assert fake_ai.requests == []  # The request saves first; no inline provider inference.
    await finish_upload(response.json(), db)
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
    assert source.status == "indexed"  # local compilation is the whole pipeline
    assert source.provider_item_id is None
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
    await finish_upload(body, db)
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
    monkeypatch.setattr(jobs, "compile_source_knowledge", fail)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/{source.id}/compile",
        headers=auth_headers,
        json={"processing_mode": "ai_verified"},
    )
    assert response.status_code == 200
    await finish_upload(response.json(), db)
    current = await client.get(f"/api/v1/knowledge/{kb.id}", headers=auth_headers)
    assert current.json()["approval_status"] == "draft"
    assert current.json()["sources"][0]["status"] == "failed"
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
    await finish_upload(added.json(), db)
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
        f"/api/v1/knowledge/{kb.id}/sources/{source_id}/compile",
        headers=auth_headers,
        json={"processing_mode": "fast"},
    )
    assert response.status_code == 409
    assert (
        await get_source(db, str(source_id))
    ).content == "A newer user edit that must be preserved."


@pytest.mark.asyncio
async def test_pdf_compiler_failure_leaves_no_source_behind(
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

    monkeypatch.setattr(endpoint, "compile_source_knowledge", fail)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/pdf",
        headers=auth_headers,
        files={"media": ("clinic.pdf", b"%PDF-original", "application/pdf")},
        data={"processing_mode": "ai_verified"},
    )
    assert response.status_code == 422
    current = await client.get(f"/api/v1/knowledge/{kb.id}", headers=auth_headers)
    assert current.json()["source_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "length,expected", [(120_001, 2), (217_001, 2), (218_500, 2), (325_501, 3)]
)
async def test_segment_overlap_never_creates_an_already_covered_tail(monkeypatch, length, expected):
    segments = []

    async def compile_segment(**kwargs):
        segments.append(kwargs["text"])
        return compiler._deterministic_structure(title="Large", url="", text=kwargs["text"]), 1, 1

    monkeypatch.setattr(compiler, "_compile_ai", compile_segment)
    source = "x" * (length - 4) + "TAIL"
    await compiler._compile_complete_source(
        title="Large", url="", text=source, api_key="fake", model="fake", client=None
    )
    assert len(segments) == expected
    assert segments[-1].endswith("TAIL")
    assert all(len(segment) > 1_500 for segment in segments)
    reconstructed = segments[0] + "".join(segment[1_500:] for segment in segments[1:])
    assert reconstructed == source


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["text", "pdf", "compile"])
async def test_all_ai_ingestion_routes_share_tenant_limit_before_inference(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
    fake_ai,
    route,
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
    limits = []

    async def deny(request, **kwargs):
        limits.append(kwargs)
        raise HTTPException(status_code=429, detail="Compilation rate exceeded")

    monkeypatch.setattr(endpoint, "enforce_rate_limit", deny)
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
    root = f"/api/v1/knowledge/{kb.id}/sources"
    if route == "pdf":
        response = await client.post(
            f"{root}/pdf",
            headers=auth_headers,
            files={"media": ("clinic.pdf", b"%PDF-original", "application/pdf")},
        )
    elif route == "text":
        response = await client.post(
            f"{root}/text", headers=auth_headers, json={"name": "New FAQ", "content": RAW}
        )
    else:
        response = await client.post(f"{root}/{source.id}/compile", headers=auth_headers, json={})
    assert response.status_code == 429
    assert fake_ai.requests == []
    assert len(limits) == 1
    assert limits[0]["scope"] == "knowledge-source-compile"
    assert limits[0]["subject"] == str(tenant.id)
    assert limits[0]["bind_to_client"] is False
    assert limits[0]["limit"] == 6
    assert limits[0]["window_seconds"] == 60


@pytest.mark.asyncio
async def test_inference_does_not_hold_a_database_transaction(
    client, auth_headers, tenant, db, monkeypatch
):
    kb = await create_kb(db, tenant)
    original = endpoint._compile_uploaded_content

    async def inspect_compile(session, *args, **kwargs):
        async def check(**compile_kwargs):
            assert not session.in_transaction()
            return await compiler.compile_source_knowledge(
                **{**compile_kwargs, "requested_mode": "fast"}
            )

        monkeypatch.setattr(endpoint, "compile_source_knowledge", check)
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(endpoint, "_compile_uploaded_content", inspect_compile)
    response = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW, "processing_mode": "fast"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_background_submission_deduplicates_and_blocks_early_approval(
    client,
    auth_headers,
    tenant,
    db,
    fake_ai,
):
    kb = await create_kb(db, tenant)
    payload = {"name": "FAQ", "content": RAW, "processing_mode": "ai_verified"}
    url = f"/api/v1/knowledge/{kb.id}/sources/text"
    first = await client.post(url, headers=auth_headers, json=payload)
    second = await client.post(url, headers=auth_headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert second.json()["source_count"] == 1
    assert first.json()["sources"][0]["id"] == second.json()["sources"][0]["id"]
    assert (
        first.json()["sources"][0]["source_metadata"]
        == second.json()["sources"][0]["source_metadata"]
    )
    assert fake_ai.requests == []
    source = await get_source(db, first.json()["sources"][0]["id"])
    assert source.raw_content == RAW and source.content is None
    blocked = await client.post(
        f"/api/v1/knowledge/{kb.id}/approval", headers=auth_headers, json={"approved": True}
    )
    assert blocked.status_code == 409
    await finish_upload(first.json(), db)
    await finish_upload(first.json(), db)  # Duplicate broker delivery is a no-op.
    assert len(fake_ai.requests) == 1


@pytest.mark.asyncio
async def test_failed_background_job_retries_in_place_and_fences_old_completion(
    client,
    auth_headers,
    tenant,
    db,
    fake_ai,
    monkeypatch,
):
    kb = await create_kb(db, tenant)
    url = f"/api/v1/knowledge/{kb.id}/sources/text"
    payload = {"name": "FAQ", "content": RAW}
    first = await client.post(url, headers=auth_headers, json=payload)
    compile_ok = jobs.compile_source_knowledge

    async def fail(**kwargs):
        raise RuntimeError("secret-provider-response-must-not-be-exposed")

    monkeypatch.setattr(jobs, "compile_source_knowledge", fail)
    await finish_upload(first.json(), db)
    failed = await client.get(f"/api/v1/knowledge/{kb.id}", headers=auth_headers)
    assert failed.json()["sources"][0]["status"] == "failed"
    assert "secret-provider" not in failed.text
    second = await client.post(url, headers=auth_headers, json=payload)
    assert second.json()["source_count"] == 1
    old = first.json()["sources"][0]
    current = second.json()["sources"][0]
    assert old["id"] == current["id"]
    old_run = old["source_metadata"]["upload_compile"]["run_id"]
    assert old_run != current["source_metadata"]["upload_compile"]["run_id"]
    await jobs._finish(str(tenant.id), str(kb.id), old["id"], old_run, error="old failure")
    assert (await get_source(db, old["id"])).status == "processing"
    monkeypatch.setattr(jobs, "compile_source_knowledge", compile_ok)
    await finish_upload(second.json(), db)
    assert (await get_source(db, old["id"])).status == "indexed"


@pytest.mark.asyncio
async def test_upload_outbox_retries_lost_enqueue_and_marks_stale_processing(
    client,
    auth_headers,
    tenant,
    db,
    fake_ai,
    monkeypatch,
):
    kb = await create_kb(db, tenant)
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW},
    )
    source_id = added.json()["sources"][0]["id"]
    published = []

    def unavailable(**kwargs):
        published.append(kwargs)
        raise ConnectionError("broker offline")

    monkeypatch.setattr(jobs.compile_upload, "apply_async", unavailable)
    await jobs._sweep()
    assert len(published) == 1
    assert published[0]["retry"] is False
    await jobs._sweep()
    assert len(published) == 1  # Enqueue lease suppresses hot-loop duplicates.
    source = await get_source(db, source_id)
    job = jobs.job_for(source)
    job["enqueued_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    jobs.set_job(source, job)
    await db.commit()
    await jobs._sweep()
    assert len(published) == 2
    source = await get_source(db, source_id)
    job = jobs.job_for(source)
    job.update(
        status="processing", started_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    )
    jobs.set_job(source, job)
    await db.commit()
    await jobs._sweep()
    source = await get_source(db, source_id)
    assert source.status == "failed"
    assert source.raw_content == RAW
    assert "timed out" in source.error_message
    assert fake_ai.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "revoke_editor", "wrong_tenant"])
async def test_background_job_rechecks_access_and_deletion(
    client,
    auth_headers,
    tenant,
    user,
    db,
    fake_ai,
    change,
):
    kb = await create_kb(db, tenant)
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW},
    )
    source = await get_source(db, added.json()["sources"][0]["id"])
    if change == "delete":
        await db.delete(source)
        await db.commit()
    elif change == "revoke_editor":
        user.is_active = False
        await db.commit()
    if change == "wrong_tenant":
        job = jobs.job_for(source)
        await db.commit()
        await jobs._compile(str(uuid4()), str(kb.id), str(source.id), job["run_id"])
    else:
        await finish_upload(added.json(), db)
    assert fake_ai.requests == []


@pytest.mark.asyncio
async def test_background_original_preview_available_before_compilation(
    client,
    auth_headers,
    tenant,
    db,
):
    kb = await create_kb(db, tenant)
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW},
    )
    source_id = added.json()["sources"][0]["id"]
    preview = await client.get(
        f"/api/v1/knowledge/{kb.id}/sources/{source_id}/preview", headers=auth_headers
    )
    assert preview.status_code == 200
    assert preview.json()["raw_text"] == RAW
    assert preview.json()["compiled_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "ai_verified"])
async def test_background_credential_error_preserves_mode_contract(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
    mode,
):
    kb = await create_kb(db, tenant)

    async def unavailable(*args, **kwargs):
        raise jobs.ProviderCredentialError("cannot decrypt")

    monkeypatch.setattr(jobs, "load_provider_config", unavailable)
    monkeypatch.setattr(jobs.settings, "openai_api_key", "")
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text",
        headers=auth_headers,
        json={"name": "FAQ", "content": RAW, "processing_mode": mode},
    )
    await finish_upload(added.json(), db)
    source = await get_source(db, added.json()["sources"][0]["id"])
    assert source.raw_content == RAW
    if mode == "automatic":
        assert source.status == "indexed"
        assert source.structured_content["compiler"]["warning"]
        assert source.structured_content["compiler"]["effective_mode"] == "fast"
    else:
        assert source.status == "failed"
        assert source.content is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["add", "compile"])
async def test_fast_retry_recovers_failed_background_source(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
    route,
):
    kb = await create_kb(db, tenant)

    async def fail(**kwargs):
        raise RuntimeError("failed AI")

    monkeypatch.setattr(jobs, "compile_source_knowledge", fail)
    payload = {"name": "FAQ", "content": RAW}
    added = await client.post(
        f"/api/v1/knowledge/{kb.id}/sources/text", headers=auth_headers, json=payload
    )
    await finish_upload(added.json(), db)
    source_id = added.json()["sources"][0]["id"]
    if route == "add":
        url, body = (
            f"/api/v1/knowledge/{kb.id}/sources/text",
            {**payload, "processing_mode": "fast"},
        )
    else:
        url, body = (
            f"/api/v1/knowledge/{kb.id}/sources/{source_id}/compile",
            {"processing_mode": "fast"},
        )
    retried = await client.post(url, headers=auth_headers, json=body)
    assert retried.status_code == 200
    result = retried.json()
    assert result["source_count"] == 1
    assert result["sync_status"] == "ready"
    assert result["sources"][0]["status"] == "indexed"
    assert result["sources"][0]["error_message"] is None
