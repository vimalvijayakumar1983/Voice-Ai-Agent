"""Single-use correlation capability for outbound Twilio callbacks.

Twilio's request signature authenticates an account, but it does not prove that
a callback path containing a VAV call UUID belongs to the provider call making
the request.  A fresh, high-entropy capability is therefore embedded in both
callback URLs for every direct Twilio dispatch.  Only its digest is persisted.

The capability is needed only while the provider CallSid is unbound.  Once a
CallSid wins the row-locked bind, every later callback is matched against that
immutable provider identity instead.
"""

import hashlib
import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TWILIO_CALLBACK_CLAIM_METADATA_KEY = "twilio_callback_claim"
TWILIO_CALLBACK_CLAIM_QUERY_PARAMETER = "vav_callback_claim"
TWILIO_CALLBACK_CLAIM_VERSION = 1
_TWILIO_CALLBACK_CLAIM_DOMAIN = b"vav.twilio.callback-claim.v1\x00"


def _claim_digest(token: str) -> str:
    return hashlib.sha256(_TWILIO_CALLBACK_CLAIM_DOMAIN + token.encode("utf-8")).hexdigest()


def create_twilio_callback_claim() -> tuple[str, dict[str, object]]:
    """Return a 256-bit URL capability and its safe persisted representation."""

    token = secrets.token_urlsafe(32)
    return token, {
        "version": TWILIO_CALLBACK_CLAIM_VERSION,
        "sha256": _claim_digest(token),
        "state": "pending",
        "created_at": datetime.now(UTC).isoformat(),
    }


def append_twilio_callback_claim(url: str, token: str) -> str:
    """Append the capability without discarding any existing query fields."""

    split = urlsplit(url)
    query = parse_qsl(split.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != TWILIO_CALLBACK_CLAIM_QUERY_PARAMETER]
    query.append((TWILIO_CALLBACK_CLAIM_QUERY_PARAMETER, token))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def twilio_callback_claim_matches(metadata: dict, supplied_token: str | None) -> bool:
    """Constant-time check of a supplied capability against persisted metadata."""

    if not supplied_token:
        return False
    claim = metadata.get(TWILIO_CALLBACK_CLAIM_METADATA_KEY)
    if (
        not isinstance(claim, dict)
        or claim.get("version") != TWILIO_CALLBACK_CLAIM_VERSION
        or claim.get("state") != "pending"
    ):
        return False
    expected = str(claim.get("sha256") or "").strip().lower()
    if len(expected) != 64:
        return False
    actual = _claim_digest(supplied_token)
    return secrets.compare_digest(actual, expected)


def mark_twilio_callback_claim_bound(
    metadata: dict | None,
    *,
    source: str,
) -> dict:
    """Record how the first CallSid was bound without persisting the capability."""

    updated = dict(metadata or {})
    claim = updated.get(TWILIO_CALLBACK_CLAIM_METADATA_KEY)
    if not isinstance(claim, dict) or claim.get("state") == "bound":
        return updated
    updated[TWILIO_CALLBACK_CLAIM_METADATA_KEY] = {
        **claim,
        "state": "bound",
        "bound_via": source,
        "bound_at": datetime.now(UTC).isoformat(),
    }
    return updated
