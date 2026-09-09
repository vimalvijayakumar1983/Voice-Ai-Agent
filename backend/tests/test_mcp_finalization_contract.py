from unittest.mock import AsyncMock

import pytest
from livekit.agents import llm

from app.livekit_runtime.mcp_answer_flow import social_closing
from app.livekit_runtime.worker import _transcript_full_text
from tests.test_mcp_answer_flow import setup_flow
from tests.test_mcp_delivery import context


def test_structured_error_does_not_break_transcript_finalization():
    flow, _, entries, _ = setup_flow()
    flow.failure(TimeoutError())
    turns = [
        {"role": "user", "content": "Thank you. Goodbye."},
        {"role": "assistant", "content": "Thank you. Goodbye."},
        *entries,
        {"role": "runtime_event", "event": "legacy_error_without_content"},
        {"role": "analysis_candidate", "content": "unspoken draft"},
        {"role": "analysis", "content": "internal reasoning"},
        {"role": "source", "content": "Original authorized report"},
        {"role": "assistant", "content": None},
        None,
    ]
    assert entries[0]["error_type"] == "TimeoutError"
    assert entries[0]["content"]
    assert _transcript_full_text(turns) == (
        "user: Thank you. Goodbye.\nassistant: Thank you. Goodbye.\n"
        "source: Original authorized report"
    )


@pytest.mark.parametrize("question", ["Thank you. Goodbye.", "Goodbye!", "Thanks, bye."])
async def test_fixed_closing_does_not_depend_on_verifier_or_trust_generated_text(question):
    verifier = AsyncMock(side_effect=TimeoutError())
    flow, turns, entries, _ = setup_flow(verifier)
    turns.append(question)
    ctx = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Your debt is zero and your payment is complete.", ["stale-id"])
    verifier.assert_not_awaited()
    assert ctx.session.spoken[0][0] == "Thank you. Goodbye."
    assert entries[0]["validation"] == "fixed_social_closing"


@pytest.mark.parametrize(
    "question",
    [
        "Thanks, what were sales?",
        "Goodbye, but first give me purchases.",
        "Bye 1234",
        "شكرا وداعا",
        "",
        "Please say goodbye",
        "Thanks",
    ],
)
def test_mixed_or_unrecognized_requests_keep_normal_evidence_path(question):
    assert social_closing(question) is None
