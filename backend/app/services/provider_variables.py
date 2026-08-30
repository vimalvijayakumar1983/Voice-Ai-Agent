"""Validation shared by every user-authored Smallest.ai variable surface."""

import json
import math

ProviderVariables = dict[str, str | int | float | bool]


def validate_provider_variables(
    value: ProviderVariables | None,
    *,
    label: str = "Context",
) -> ProviderVariables | None:
    if value is None:
        return None
    if len(value) > 50:
        raise ValueError(f"{label} can contain at most 50 variables")
    for key, item in value.items():
        if not key or len(key) > 100:
            raise ValueError("Context variable keys must be 1-100 characters")
        if key == "_vav_call_id" or key.startswith("_voice_ai_"):
            raise ValueError("Context variable key is reserved by the platform")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Context variable numbers must be finite")
        if isinstance(item, str) and len(item) > 1_000:
            raise ValueError("Context variable strings can contain at most 1000 characters")
    if len(json.dumps(value, separators=(",", ":")).encode()) > 16_384:
        raise ValueError(f"{label} can contain at most 16 KB")
    return value
