import hashlib
import hmac

import pytest
from sqlalchemy import select

from app.api.v1.endpoints import webhooks
from app.models.agent import Agent
from app.models.call import Call, CallTranscript


def test_smallest_webhook_signature_accepts_raw_body_hmac(monkeypatch):
    secret = "webhook_test_secret"
    raw_body = b'{"metadata":{"eventType":"post-conversation"}}'
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(webhooks.settings, "smallest_webhook_secret", secret)

    assert webhooks.verify_smallest_signature(raw_body, signature)
    assert not webhooks.verify_smallest_signature(raw_body + b" ", signature)
    assert not webhooks.verify_smallest_signature(raw_body, "")


@pytest.mark.asyncio
async def test_smallest_webcall_reserved_variable_creates_local_conversation(tenant, db):
    agent = Agent(
        tenant_id=tenant.id,
        name="Browser history agent",
        system_prompt="Answer from approved knowledge.",
        voice_provider="smallest",
        provider_agent_id="browser-history-agent",
    )
    db.add(agent)
    await db.flush()

    effects = await webhooks._process_smallest_webhook(
        db,
        {
            "id": "browser-history-delivery",
            "metadata": {
                "agentId": agent.provider_agent_id,
                "callId": "browser-history-call",
                "eventType": "post-conversation",
                "variables": {"conversation_type": "webcall"},
                "callData": {"callStatus": "completed", "callDuration": 17},
                "transcript": [
                    {"role": "user", "content": "Which doctors are available?"},
                    {"role": "assistant", "content": "I can help with the doctor directory."},
                ],
            },
        },
    )
    await db.commit()

    call = await db.scalar(select(Call).where(Call.provider_call_sid == "browser-history-call"))
    assert call is not None
    assert call.direction == "inbound"
    assert call.status == "completed"
    assert call.duration_seconds == 17
    assert call.call_metadata["conversation_type"] == "webcall"
    assert call.call_metadata["channel"] == "browser"
    transcript = await db.scalar(select(CallTranscript).where(CallTranscript.call_id == call.id))
    assert transcript is not None
    assert "Which doctors are available?" in transcript.full_text
    assert effects.process_call_id == str(call.id)
