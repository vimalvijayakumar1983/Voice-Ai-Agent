from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1.endpoints.knowledge import (
    _provider_source_status,
    _provider_url_key,
    _reconcile_provider_sources,
)


def _knowledge(source):
    return SimpleNamespace(
        sources=[source],
        source_count=1,
        indexed_source_count=0,
        sync_status="processing",
        sync_error=None,
    )


def _url_source(location="https://www.aesmc.com/"):
    return SimpleNamespace(
        source_type="url",
        name=location,
        location=location,
        status="processing",
        provider_item_id=None,
        last_synced_at=None,
        error_message=None,
    )


def test_provider_url_key_handles_provider_trailing_slash_normalization():
    assert _provider_url_key("HTTPS://WWW.AESMC.COM") == _provider_url_key("https://www.aesmc.com/")
    assert _provider_url_key("https://www.aesmc.com/services/") == _provider_url_key(
        "https://www.aesmc.com/services"
    )


def test_provider_completed_status_aliases_are_indexed():
    for value in ("complete", "completed", "indexed", "processed", "ready", "success"):
        assert _provider_source_status({"processingStatus": value}) == "indexed"


def test_provider_processing_status_takes_precedence_over_response_status():
    assert _provider_source_status({"status": True, "processingStatus": "completed"}) == "indexed"


def test_reconcile_matches_normalized_provider_url():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[
            {
                "_id": "provider-source-1",
                "url": "https://www.aesmc.com",
                "status": "indexed",
            }
        ],
        items=[],
        provider_knowledge_base={"processingStatus": "completed"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.provider_item_id == "provider-source-1"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1


def test_reconcile_matches_completed_url_returned_as_knowledge_item():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[
            {
                "_id": "provider-item-1",
                "processingStatus": "completed",
                "metadata": {"sourceUrl": "https://www.aesmc.com"},
            }
        ],
        provider_knowledge_base={"processingStatus": "processing"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.provider_item_id == "provider-item-1"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1


def test_reconcile_uses_completed_knowledge_base_as_safe_fallback():
    source = _url_source()
    knowledge = _knowledge(source)
    now = datetime.now(UTC)

    _reconcile_provider_sources(
        knowledge,
        scraped=[],
        items=[],
        provider_knowledge_base={"processingStatus": "completed"},
        now=now,
    )

    assert source.status == "indexed"
    assert source.last_synced_at == now
    assert knowledge.sync_status == "ready"
    assert knowledge.indexed_source_count == 1
