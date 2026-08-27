import hashlib
import hmac

from app.api.v1.endpoints import webhooks


def test_smallest_webhook_signature_accepts_raw_body_hmac(monkeypatch):
    secret = "webhook_test_secret"
    raw_body = b'{"metadata":{"eventType":"post-conversation"}}'
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(webhooks.settings, "smallest_webhook_secret", secret)

    assert webhooks.verify_smallest_signature(raw_body, signature)
    assert not webhooks.verify_smallest_signature(raw_body + b" ", signature)
    assert not webhooks.verify_smallest_signature(raw_body, "")
