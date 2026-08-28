from app.services.agent_catalog_cache import AgentCatalogSnapshotCache


def _usable_voice(voice_id: str) -> dict:
    return {
        "id": voice_id,
        "languages": ["en"],
        "synthesizer_model": "waves_lightning_v3_1",
    }


def test_catalog_cache_keeps_a_fresh_defensive_last_known_good_snapshot():
    now = [100.0]
    cache = AgentCatalogSnapshotCache(ttl_seconds=30, clock=lambda: now[0])
    voices = [_usable_voice("jordan")]

    assert cache.remember("public", voices) is True
    voices[0]["id"] = "mutated-source"
    first = cache.get("public")
    assert first == [_usable_voice("jordan")]

    assert first is not None
    first[0]["id"] = "mutated-result"
    assert cache.get("public") == [_usable_voice("jordan")]

    now[0] = 130.0
    assert cache.get("public") is None


def test_catalog_cache_rejects_empty_or_unusable_snapshots_and_is_lru_bounded():
    cache = AgentCatalogSnapshotCache(max_entries=2)

    assert cache.remember("empty", []) is False
    assert cache.remember("unknown", [{"id": "unknown", "synthesizer_model": None}]) is False
    assert cache.get("empty") is None
    assert cache.get("unknown") is None

    assert cache.remember("one", [_usable_voice("one")]) is True
    assert cache.remember("two", [_usable_voice("two")]) is True
    assert cache.get("one") == [_usable_voice("one")]  # refresh LRU position
    assert cache.remember("three", [_usable_voice("three")]) is True

    assert cache.get("one") == [_usable_voice("one")]
    assert cache.get("two") is None
    assert cache.get("three") == [_usable_voice("three")]
