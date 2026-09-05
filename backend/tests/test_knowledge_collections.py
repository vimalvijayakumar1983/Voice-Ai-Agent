"""A list is a scoped collection, not top-k search. No production names required."""

import pytest
from sqlalchemy import select

from app.livekit_runtime.inworld_single_pass import deterministic_grounded_reply
from app.models.agent import KnowledgeSource
from app.services.exact_fact_retrieval import ExactFactSource, build_exact_fact_index
from app.services.knowledge_collections import (
    collection_reply,
    collection_request,
    decode_collection,
    retrieve_collection,
)
from tests.test_conversation_scope import setup_runtime


def fact(subject, predicate, value):
    name = predicate.split(":", 1)[1].strip() if predicate.startswith("person profile:") else ""
    return dict(
        subject=subject,
        predicate=predicate,
        value=value,
        evidence=f"{subject}: {name} {predicate if not name else ''} {value}.",
        search_phrases=[],
    )


def index(facts, revision="r1"):
    return build_exact_fact_index(
        (
            ExactFactSource(
                source_id="source", source_name="Directory", structured_content={"facts": facts}
            ),
        ),
        revision=revision,
    )


def page(built, category, offset=0, company="Harbour Group"):
    return retrieve_collection(
        built.collection_records,
        company=company,
        category=category,
        query="List all " + category,
        revision=built.revision,
        offset=offset,
    )


def test_director_roles_and_profiles_are_merged_without_losing_board_member():
    built = index(
        [
            fact("Harbour Group", "Director", "Alice Jones"),
            fact("Harbour Group", "person profile: Alice Jones", "Director"),
            fact("Harbour Group", "Managing Director", "Beth Smith"),
            fact("Harbour Group", "person profile: Cara Brown", "Director board member and CFO"),
            fact("Harbour Group", "President", "Dina Rose"),
            fact("Harbour Group", "Former Director", "Erin Grey"),
            fact("Other Company", "Director", "Farah White"),
        ]
    )
    result = page(built, "directors")
    assert [i.name for i in result.items] == ["Alice Jones", "Beth Smith", "Cara Brown"]
    assert result.total == 3 and result.coverage == "indexed_only"
    assert "Dina" not in collection_reply(result) and "Farah" not in collection_reply(result)
    assert deterministic_grounded_reply(result.encode()) == collection_reply(result)


@pytest.mark.parametrize(
    "category,predicate",
    [
        ("services", "service offering"),
        ("divisions", "business segment"),
        ("branches", "branch name"),
    ],
)
def test_every_page_is_retrievable_deduplicated_and_stable(category, predicate):
    facts = [fact("Harbour Group", predicate, f"Entry {i:02}") for i in range(13)]
    built = index(facts + [facts[0]])
    pages = [page(built, category, offset) for offset in [0, 5, 10, 13]]
    assert [p.total for p in pages] == [13] * 4
    assert [p.next_offset for p in pages] == [5, 10, 13, 13]
    assert [i.name for p in pages for i in p.items] == [f"Entry {i:02}" for i in range(13)]
    assert "Say next" in collection_reply(pages[0])
    assert "end of" in collection_reply(pages[3])
    assert page(index(list(reversed(facts))), category).items == page(index(facts), category).items


def test_no_branch_inference_from_other_company_contact():
    built = index(
        [
            fact("Other Company", "address", "Some Street"),
            fact("Harbour Group", "primary telephone", "+971 2 123 4567"),
        ]
    )
    result = page(built, "branches")
    assert result.total == 0 and "don't have a published" in collection_reply(result)


def test_unsupported_person_in_profile_and_ungrounded_value_are_excluded():
    bad = fact("Harbour Group", "person profile: Alice Jones", "Director")
    bad["evidence"] = "Harbour Group Director Beth Smith."
    bad2 = fact("Harbour Group", "branch name", "Secret Branch")
    bad2["evidence"] = "Harbour Group has an office."
    built = index([bad, bad2])
    assert not built.collection_records


def test_index_revision_update_does_not_modify_old_list():
    old = index([fact("Harbour Group", "branch name", "North Branch")], "old")
    new = index([fact("Harbour Group", "branch name", "South Branch")], "new")
    assert page(old, "branches").items[0].name == "North Branch"
    assert page(new, "branches").items[0].name == "South Branch"
    assert page(old, "branches").revision != page(new, "branches").revision


def test_hard_truncated_list_never_presents_partial_data_as_complete():
    result = page(index([fact("Harbour Group", "Director", "Alice Jones")]), "directors")
    result.blocked = True
    assert "not fully loaded" in collection_reply(result)
    assert "Alice" not in collection_reply(result)


def test_filtered_list_request_is_not_silently_answered_unfiltered():
    category, clarification = collection_request("List all branches in Dubai", "Harbour Group")
    assert category is None and "filter" in clarification
    category, clarification = collection_request("List directors and branches", "Harbour Group")
    assert category is None and "first" in clarification


def test_count_is_explicitly_a_published_index_count_not_a_real_world_assertion():
    built = index([fact("Harbour Group", "Director", "Alice Jones")])
    result = retrieve_collection(
        built.collection_records,
        company="Harbour Group",
        category="directors",
        query="How many directors?",
        revision="r1",
    )
    assert "published knowledge lists 1" in collection_reply(result)


def test_directory_cannot_treat_directors_message_as_a_person():
    built = index([fact("Harbour Group", "director message", "Welcome to our company")])
    assert not built.collection_records


def test_service_fallback_labels_divisions_without_relabeling_as_services():
    built = index([fact("Harbour Group", "business segment", "Trading")])
    result = page(built, "services")
    assert result.category == "divisions" and result.related_category
    assert "rather than a detailed services list" in collection_reply(result)
    assert "Trading" in collection_reply(result)


def test_most_specific_duplicate_role_is_kept_once():
    built = index(
        [
            fact("Harbour Group", "Chief Financial Officer", "Alice Jones"),
            fact(
                "Harbour Group",
                "person profile: Alice Jones",
                "Director board member and Chief Financial Officer",
            ),
        ]
    )
    result = page(built, "leadership")
    assert len(result.items) == 1 and len(result.items[0].roles) == 1


async def test_collection_telemetry_records_page_and_source_ids(db, tenant, monkeypatch):
    from app.livekit_runtime.worker import _LiveKitRuntimeTelemetry

    runtime = await runtime_fixture(db, tenant, monkeypatch)
    runtime._telemetry = _LiveKitRuntimeTelemetry({}, [], 0)
    await runtime.retrieve_single_pass_evidence("List all directors")
    trace = runtime._telemetry.current_turn_trace
    assert trace["knowledge_retrieval_path"] == "collection"
    assert trace["collection_total"] == 8 and trace["collection_next_offset"] == 5
    assert trace["collection_coverage"] == "indexed_only"
    assert len(trace["collection_evidence_ids"]) == 5


@pytest.mark.parametrize(
    "query,category",
    [
        ("What about the directors?", "directors"),
        ("Who are all the directors?", "directors"),
        ("List every branch", "branches"),
        ("Where are your branches?", "branches"),
        ("What services do you offer?", "services"),
        ("How many divisions?", "divisions"),
        ("What are the business divisions?", "divisions"),
        ("List the board of directors", "directors"),
        ("Who is the chairman?", ""),
        ("Tell me about healthcare services", ""),
    ],
)
def test_collection_intent_does_not_replace_specific_question(query, category):
    found, _ = collection_request(query, "Harbour Group")
    assert found == (category or None)


async def runtime_fixture(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(db, tenant, monkeypatch, "Harbour Group", "Harbour Trading")
    runtime._conversation_routing_v2 = runtime._collections_enabled = True
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant.id))
    source.structured_content = {
        "facts": [
            *source.structured_content["facts"],
            *[fact("Harbour Group", "Director", f"Person {i}") for i in range(8)],
            fact("Harbour Trading", "Director", "Trading Person"),
        ]
    }
    await db.commit()
    return runtime


async def test_runtime_pagination_advances_only_after_complete_speech(db, tenant, monkeypatch):
    runtime = await runtime_fixture(db, tenant, monkeypatch)
    evidence = await runtime.retrieve_single_pass_evidence("List all directors")
    result = decode_collection(evidence)
    assert result.total == 8 and result.next_offset == 5
    remember = runtime.prepare_spoken_response("List all directors", evidence)
    remember("The published directors list")
    assert runtime._collection_cursor is None
    remember(collection_reply(result))
    continuation = await runtime.retrieve_single_pass_evidence("next")
    second = decode_collection(continuation)
    assert second.offset == 5 and len(second.items) == 3
    assert all(i.name not in {j.name for j in result.items} for i in second.items)
    runtime.prepare_spoken_response("next", continuation)(collection_reply(second))
    assert "end of" in deterministic_grounded_reply(
        await runtime.retrieve_single_pass_evidence("next")
    )


async def test_company_switch_clears_cursor_and_stale_commit_cannot_restore_it(
    db, tenant, monkeypatch
):
    runtime = await runtime_fixture(db, tenant, monkeypatch)
    evidence = await runtime.retrieve_single_pass_evidence("List all directors")
    remember = runtime.prepare_spoken_response("List all directors", evidence)
    await runtime.retrieve_single_pass_evidence("About Harbour Trading")
    remember(deterministic_grounded_reply(evidence))
    assert runtime._collection_cursor is None
    assert "Which list" in await runtime.retrieve_single_pass_evidence("next")
    result = decode_collection(await runtime.retrieve_single_pass_evidence("List all directors"))
    assert [i.name for i in result.items] == ["Trading Person"]


async def test_unrelated_question_clears_pending_list(db, tenant, monkeypatch):
    runtime = await runtime_fixture(db, tenant, monkeypatch)
    evidence = await runtime.retrieve_single_pass_evidence("List all directors")
    runtime.prepare_spoken_response("List all directors", evidence)(
        deterministic_grounded_reply(evidence)
    )
    await runtime.retrieve_single_pass_evidence("What is the phone number?")
    assert runtime._collection_cursor is None
