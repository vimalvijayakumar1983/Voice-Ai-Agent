from app.services.knowledge_retrieval import _RETRIEVAL_SCOPE_NOTE
from app.services.knowledge_search import _context_chunks


def test_scope_guidance_is_metadata_not_a_source():
    chunks, note = _context_chunks(
        "Contextual terminology considered: Laser.\n\n"
        + _RETRIEVAL_SCOPE_NOTE
        + "Source: Home\nLaser hair removal.\n\nSource: Offers\nFiller offer.",
    )
    assert [c.source for c in chunks] == ["Home", "Offers"]
    assert "Retrieval scope:" in note
    assert "Contextual terminology" in note


def test_directory_aggregate_is_presented_as_derived_not_a_file():
    chunks, note = _context_chunks(
        '{"published_name_count":34,"qualification":"Published names, not current staff."}'
    )
    assert chunks[0].source == "Approved directory aggregate"
    assert "34" in chunks[0].text
    assert "not current staff" in note
