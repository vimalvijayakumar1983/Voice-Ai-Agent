"""Security helpers for user-configured integration destinations and credentials."""

from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class IntegrationConfigError(ValueError):
    """Raised when an integration configuration is unsafe or malformed."""


class IntegrationConfigUnavailableError(IntegrationConfigError):
    """Raised when an encrypted configuration cannot be authenticated.

    The message is intentionally generic so callers can fail closed without
    disclosing ciphertext, key material, or decrypted configuration values.
    """


INTEGRATION_CONFIG_ENVELOPE_PREFIX = "fernet:v1:"
# Storage policy v2 replaces heuristic redaction with an allowlist-only public
# projection. This is independent from the Fernet envelope format version.
INTEGRATION_CONFIG_STORAGE_VERSION = 2
PUBLIC_URL_REDACTION_PLACEHOLDER = "https://redacted.invalid/"
_INTEGRATION_KEY_CONTEXT = b"voice-ai-agent/integration-config/fernet-v1\x00"
SUPPORTED_WEBHOOK_EVENTS = {
    "*",
    "call.completed",
    "call.analytics_updated",
}


def _fernet_from_key_material(key_material: str) -> Fernet:
    """Derive a purpose-separated Fernet key from application key material."""
    derived_key = hashlib.sha256(_INTEGRATION_KEY_CONTEXT + key_material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def _integration_fernet() -> Fernet:
    """Build the primary Fernet used for new envelopes."""
    key_material = settings.integration_encryption_key.strip() or settings.secret_key.strip()
    if not key_material:
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable")
    return _fernet_from_key_material(key_material)


def _integration_decryption_fernets() -> list[Fernet]:
    """Return the dedicated key plus SECRET_KEY transition fallback.

    Trying the compatibility key after the dedicated key lets operators set a
    dedicated key and rewrap older fallback-encrypted rows without downtime.
    It is intentionally not a general multi-key rotation facility.
    """
    dedicated_key = settings.integration_encryption_key.strip()
    compatibility_key = settings.secret_key.strip()
    key_materials = [dedicated_key] if dedicated_key else []
    if compatibility_key and compatibility_key != dedicated_key:
        key_materials.append(compatibility_key)
    if not key_materials:
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable")
    return [_fernet_from_key_material(key_material) for key_material in key_materials]


def encrypt_integration_config(config: Mapping[str, Any]) -> str:
    """Serialize and authenticate-encrypt a complete integration config."""
    try:
        payload = json.dumps(
            dict(config),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        token = _integration_fernet().encrypt(payload).decode("ascii")
    except IntegrationConfigUnavailableError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable") from exc
    return f"{INTEGRATION_CONFIG_ENVELOPE_PREFIX}{token}"


def decrypt_integration_config(encrypted_config: str) -> dict[str, Any]:
    """Authenticate and decrypt one supported configuration envelope."""
    if not isinstance(encrypted_config, str) or not encrypted_config.startswith(
        INTEGRATION_CONFIG_ENVELOPE_PREFIX
    ):
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable")
    try:
        token = encrypted_config.removeprefix(INTEGRATION_CONFIG_ENVELOPE_PREFIX).encode("ascii")
    except UnicodeError as exc:
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable") from exc

    plaintext: bytes | None = None
    try:
        for fernet in _integration_decryption_fernets():
            try:
                plaintext = fernet.decrypt(token)
                break
            except InvalidToken:
                continue
    except IntegrationConfigUnavailableError:
        raise
    if plaintext is None:
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable")

    try:
        value = json.loads(plaintext)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable") from exc
    if not isinstance(value, dict):
        raise IntegrationConfigUnavailableError("Integration configuration is unavailable")
    return value


def load_integration_config(
    public_config: Mapping[str, Any] | None,
    encrypted_config: str | None,
) -> dict[str, Any]:
    """Load the full config, with a bounded compatibility path for legacy rows.

    A present envelope is authoritative and never falls back to JSONB if its
    authentication fails. Rows created before envelope encryption have a null
    envelope and are treated as legacy until their first control-plane mutation.
    """
    if encrypted_config is not None:
        return decrypt_integration_config(encrypted_config)
    return copy.deepcopy(dict(public_config or {}))


_SECRET_NAMES = {
    "api_key",
    "apikey",
    "connection_string",
    "dsn",
    "auth_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "private_key",
    "private_token",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "signing_key",
    "signing_secret",
    "token",
}
_SECRET_SUFFIXES = (
    "_api_key",
    "_auth_token",
    "_client_secret",
    "_password",
    "_private_key",
    "_private_token",
    "_secret",
    "_secret_access_key",
    "_secret_key",
    "_signing_key",
    "_signing_secret",
    "_token",
)
_SECRET_PLACEHOLDERS = {
    "",
    "***",
    "*****",
    "********",
    "[redacted]",
    "<redacted>",
    "__redacted__",
    "redacted",
}
_URL_NAMES = {
    "base_url",
    "callback",
    "callback_url",
    "endpoint",
    "endpoint_url",
    "url",
    "webhook",
    "webhook_url",
}
_BLOCKED_HOSTS = {
    "0",
    "instance-data",
    "localhost",
    "localhost.localdomain",
    "localtest.me",
    "lvh.me",
    "metadata",
    "nip.io",
    "sslip.io",
    "vcap.me",
}
_BLOCKED_HOST_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".intranet",
    ".lan",
    ".local",
    ".localhost",
    ".localdomain",
    ".localtest.me",
    ".lvh.me",
    ".nip.io",
    ".sslip.io",
    ".test",
    ".vcap.me",
)
_NORMALIZE_KEY_PATTERN = re.compile(r"[^a-z0-9]+")


def normalize_config_key(key: object) -> str:
    """Normalize common JSON key styles for security classification."""
    value = str(key).strip()
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return _NORMALIZE_KEY_PATTERN.sub("_", value.lower()).strip("_")


def is_secret_key(key: object) -> bool:
    """Return whether a configuration key conventionally contains a credential."""
    normalized = normalize_config_key(key)
    compact = normalized.replace("_", "")
    return (
        normalized in _SECRET_NAMES
        or compact in _SECRET_NAMES
        or normalized.endswith(_SECRET_SUFFIXES)
    )


def is_secret_placeholder(value: object) -> bool:
    """Recognize UI masks and blanks that must never overwrite a stored secret."""
    return value is None or (
        isinstance(value, str) and value.strip().lower() in _SECRET_PLACEHOLDERS
    )


def is_url_placeholder(value: object) -> bool:
    """Recognize the stable write-only URL mask returned by the API."""
    return value == PUBLIC_URL_REDACTION_PLACEHOLDER or is_secret_placeholder(value)


def _is_url_key(key: object) -> bool:
    normalized = normalize_config_key(key)
    return normalized in _URL_NAMES or normalized.endswith("_url")


def _validate_hostname(hostname: str) -> None:
    host = hostname.rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise IntegrationConfigError("URL host must be a public internet host")
    if "%" in host or "\\" in host:
        raise IntegrationConfigError("URL host contains unsupported characters")

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise IntegrationConfigError("URL host is invalid") from exc

    if "." not in ascii_host and ":" not in ascii_host:
        raise IntegrationConfigError("URL host must be a fully qualified public host")
    if re.fullmatch(r"[0-9a-fx.]+", ascii_host) and "x" in ascii_host:
        raise IntegrationConfigError("URL host must not use an encoded IP address")

    # Reject alternate numeric spellings such as 127.1 or 2130706433. Standard
    # IP literals are handled below, while public DNS names must contain a letter.
    if not any(character.isalpha() for character in ascii_host):
        try:
            address = ipaddress.ip_address(ascii_host)
        except ValueError as exc:
            raise IntegrationConfigError("URL host must not use an encoded IP address") from exc
        if not address.is_global:
            raise IntegrationConfigError("URL host must resolve to a public IP address")
        return

    try:
        address = ipaddress.ip_address(ascii_host)
    except ValueError:
        return
    if not address.is_global:
        raise IntegrationConfigError("URL host must resolve to a public IP address")


def validate_public_https_url(value: str) -> str:
    """Validate an outbound integration URL against common SSRF primitives.

    This deliberately permits only HTTPS destinations, rejects embedded
    credentials and fragments, and blocks private, loopback, link-local,
    reserved, wildcard-local, and alternate numeric hosts. Callers that perform
    network requests should also resolve and pin the destination immediately
    before connecting to defend against DNS rebinding.
    """
    if not isinstance(value, str) or not value.strip():
        raise IntegrationConfigError("Integration URL must be a non-empty string")
    if value != value.strip() or any(character.isspace() for character in value):
        raise IntegrationConfigError("Integration URL must not contain whitespace")
    if "\\" in value:
        raise IntegrationConfigError("Integration URL must not contain backslashes")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise IntegrationConfigError("Integration URL is malformed") from exc

    if parsed.scheme.lower() != "https":
        raise IntegrationConfigError("Integration URL must use HTTPS")
    if not parsed.netloc or not parsed.hostname:
        raise IntegrationConfigError("Integration URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise IntegrationConfigError("Integration URL must not include credentials")
    if parsed.fragment:
        raise IntegrationConfigError("Integration URL must not include a fragment")
    if port == 0:
        raise IntegrationConfigError("Integration URL port is invalid")

    _validate_hostname(parsed.hostname)
    return value


def validate_integration_config_urls(config: Mapping[str, Any]) -> None:
    """Recursively validate URL and base-URL fields in an integration config."""

    def visit(value: object, path: str = "config") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if _is_url_key(key) and child is not None:
                    if not isinstance(child, str):
                        raise IntegrationConfigError(f"{child_path} must be a URL string")
                    try:
                        validate_public_https_url(child)
                    except IntegrationConfigError as exc:
                        raise IntegrationConfigError(f"{child_path}: {exc}") from exc
                elif isinstance(child, (Mapping, list, tuple)):
                    visit(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(config)


def merge_integration_config(
    existing: Mapping[str, Any] | None,
    update: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Deep-merge config while treating secrets as write-only replacements.

    A supplied non-placeholder secret replaces the old value. Omitting a secret,
    or submitting a blank/redaction mask commonly emitted by admin UIs, preserves
    it. This avoids both whole-config data loss and accidental credential erasure.
    """
    merged: dict[str, Any] = copy.deepcopy(dict(existing or {}))

    def apply(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
        for key, value in patch.items():
            key = str(key)
            if _is_url_key(key) and is_url_placeholder(value):
                # Public responses expose only a stable sentinel. Round-tripping
                # it must preserve the encrypted destination, never replace it.
                continue
            if is_secret_key(key):
                if not is_secret_placeholder(value):
                    target[key] = copy.deepcopy(value)
                continue
            current = target.get(key)
            if isinstance(current, Mapping) and isinstance(value, Mapping):
                nested = copy.deepcopy(dict(current))
                apply(nested, value)
                target[key] = nested
            elif isinstance(value, Mapping):
                nested: dict[str, Any] = {}
                apply(nested, value)
                target[key] = nested
            else:
                target[key] = copy.deepcopy(value)

    if update is not None:
        apply(merged, update)
    return merged


def clear_integration_secrets(config: Mapping[str, Any], paths: Sequence[str]) -> dict[str, Any]:
    """Explicitly remove write-only secrets identified by dotted config paths."""
    result: dict[str, Any] = copy.deepcopy(dict(config))
    for raw_path in paths:
        path = raw_path.strip()
        parts = path.split(".") if path else []
        if not parts or any(not part for part in parts) or not is_secret_key(parts[-1]):
            raise IntegrationConfigError(
                f"clear_secrets entry '{raw_path}' must identify a secret field"
            )

        parent: dict[str, Any] | None = result
        for part in parts[:-1]:
            child = parent.get(part) if parent is not None else None
            if not isinstance(child, dict):
                parent = None
                break
            parent = child
        if parent is not None:
            parent.pop(parts[-1], None)
    return result


def public_integration_config(
    config: Mapping[str, Any] | None,
    integration_type: str,
) -> tuple[dict[str, Any], list[str]]:
    """Return an allowlisted public projection and configured-secret paths.

    Unknown values never enter public JSONB or API responses. Destinations and
    credentials are write-only; only controlled capability and routing metadata
    is exposed to the VAV control plane.
    """
    secret_paths: list[str] = []

    def detect(value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key = str(key)
                child_path = f"{path}.{key}" if path else key
                if is_secret_key(key):
                    if not is_secret_placeholder(child):
                        secret_paths.append(child_path)
                elif _is_url_key(key):
                    if not is_url_placeholder(child):
                        secret_paths.append(child_path)
                else:
                    detect(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                detect(child, f"{path}[{index}]")

    full_config = dict(config or {})
    detect(full_config, "")
    normalized_type = integration_type.strip().lower()
    public_config: dict[str, Any] = {}

    if normalized_type == "webhook":
        events = full_config.get("events")
        if isinstance(events, list) and all(
            isinstance(event, str) and event in SUPPORTED_WEBHOOK_EVENTS for event in events
        ):
            public_config["events"] = copy.deepcopy(events)
        url = full_config.get("url")
        if isinstance(url, str) and url:
            public_config["url"] = PUBLIC_URL_REDACTION_PLACEHOLDER
            secret_paths.append("url")

    elif normalized_type in {"his_api", "vav_crm"}:
        base_url = full_config.get("base_url")
        if isinstance(base_url, str) and base_url:
            public_config["base_url"] = PUBLIC_URL_REDACTION_PLACEHOLDER
            secret_paths.append("base_url")

        auth_type = full_config.get("auth_type")
        if auth_type in {"bearer", "api_key"}:
            public_config["auth_type"] = auth_type
        api_key_header = full_config.get("api_key_header")
        if (
            auth_type == "api_key"
            and isinstance(api_key_header, str)
            and re.fullmatch(r"[A-Za-z0-9-]{1,64}", api_key_header)
        ):
            public_config["api_key_header"] = api_key_header

        capabilities: list[str] = []
        for key, capability in (
            ("availability_path", "live_availability"),
            (
                "create_path",
                "create_appointment"
                if normalized_type == "his_api"
                else "create_appointment_request",
            ),
            ("reschedule_path", "reschedule_appointment"),
            ("cancel_path", "cancel_appointment"),
        ):
            value = full_config.get(key)
            if (
                isinstance(value, str)
                and value.startswith("/")
                and "?" not in value
                and "#" not in value
            ):
                public_config[key] = value
                capabilities.append(capability)
        public_config["capabilities"] = capabilities

    elif normalized_type == "google_sheets":
        sheet_name = full_config.get("sheet_name")
        if isinstance(sheet_name, str) and sheet_name:
            public_config["sheet_name"] = sheet_name
        table_name = full_config.get("table_name")
        if isinstance(table_name, str) and table_name:
            public_config["table_name"] = table_name
        if (
            full_config.get("spreadsheet_id")
            or full_config.get("spreadsheet_configured") is True
        ):
            public_config["spreadsheet_configured"] = True
        if full_config.get("spreadsheet_id"):
            secret_paths.append("spreadsheet_id")
        public_config["capabilities"] = ["create_appointment_request"]

    return public_config, sorted(set(secret_paths))


def prepare_integration_config_storage(
    config: Mapping[str, Any],
    integration_type: str,
) -> tuple[dict[str, Any], str]:
    """Return the sanitized JSONB projection and encrypted complete config."""
    public_config, _secret_paths = public_integration_config(config, integration_type)
    return public_config, encrypt_integration_config(config)


async def backfill_legacy_integration_configs(
    db: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    batch_size: int = 250,
) -> tuple[int, int]:
    """Idempotently encrypt and sanitize one bounded batch of legacy rows.

    ``tenant_id=None`` is intended for a controlled application maintenance job;
    tenant-facing endpoints must always pass the authenticated tenant. Row locks
    with ``SKIP LOCKED`` let multiple maintenance processes coexist safely. The
    caller owns the transaction and should repeat until ``remaining`` is zero.
    """
    from sqlalchemy import func, or_, select

    from app.models.integration import Integration

    bounded_batch_size = min(max(batch_size, 1), 1000)
    requires_backfill = or_(
        Integration.encrypted_config.is_(None),
        Integration.config_encryption_version.is_(None),
        Integration.config_encryption_version != INTEGRATION_CONFIG_STORAGE_VERSION,
    )
    conditions = [requires_backfill]
    if tenant_id is not None:
        conditions.append(Integration.tenant_id == tenant_id)

    result = await db.execute(
        select(Integration)
        .where(*conditions)
        .order_by(Integration.id)
        .limit(bounded_batch_size)
        .with_for_update(skip_locked=True)
    )
    integrations = result.scalars().all()
    for integration in integrations:
        full_config = load_integration_config(
            integration.config,
            integration.encrypted_config,
        )
        public_config, encrypted_config = prepare_integration_config_storage(
            full_config,
            integration.integration_type,
        )
        integration.config = public_config
        integration.encrypted_config = encrypted_config
        integration.config_encryption_version = INTEGRATION_CONFIG_STORAGE_VERSION
    await db.flush()

    remaining = await db.scalar(select(func.count()).select_from(Integration).where(*conditions))
    return len(integrations), int(remaining or 0)
