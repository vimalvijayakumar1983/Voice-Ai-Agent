"""Single-use invariants for direct Twilio callback capabilities."""

import pytest

from app.services.twilio_callback_claim import (
    TWILIO_CALLBACK_CLAIM_METADATA_KEY,
    create_twilio_callback_claim,
    mark_twilio_callback_claim_bound,
    twilio_callback_claim_matches,
)


def test_exact_claim_matches_only_while_explicitly_pending():
    token, claim = create_twilio_callback_claim()
    metadata = {TWILIO_CALLBACK_CLAIM_METADATA_KEY: claim}

    assert twilio_callback_claim_matches(metadata, token) is True
    assert twilio_callback_claim_matches(metadata, f"wrong-{token}") is False

    bound = mark_twilio_callback_claim_bound(metadata, source="provider_callback")

    assert bound[TWILIO_CALLBACK_CLAIM_METADATA_KEY]["state"] == "bound"
    assert twilio_callback_claim_matches(bound, token) is False


@pytest.mark.parametrize("state", [None, "", "bound", "expired", "PENDING"])
def test_claim_without_exact_pending_state_fails_closed(state):
    token, claim = create_twilio_callback_claim()
    if state is None:
        claim.pop("state")
    else:
        claim["state"] = state

    assert (
        twilio_callback_claim_matches(
            {TWILIO_CALLBACK_CLAIM_METADATA_KEY: claim},
            token,
        )
        is False
    )
