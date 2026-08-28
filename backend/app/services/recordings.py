"""Private, bounded retrieval of provider-hosted call recordings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.models.call import Call
from app.providers.smallest import SmallestAIClient, SmallestAIError, get_smallest_client

MAX_RECORDING_BYTES = 32 * 1024 * 1024
RECORDING_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_TWILIO_RECORDING_PATH = re.compile(
    r"^/2010-04-01/Accounts/(AC[0-9a-fA-F]{32})/Recordings/(RE[0-9a-fA-F]{32})(?:\.(?:mp3|wav))?$"
)
_ALLOWED_RECORDING_CONTENT_TYPES = {
    "application/octet-stream": None,
    "application/ogg": "audio/ogg",
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/mp4": "audio/mp4",
    "audio/ogg": "audio/ogg",
    "audio/wav": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/x-wav": "audio/wav",
}


class RecordingError(RuntimeError):
    """A sanitized recording retrieval failure safe for an API response."""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RecordingAudio:
    content: bytes
    content_type: str
    extension: str


def _validate_https_url(url: str, *, allowed_host) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RecordingError("The recording provider returned an invalid media location.") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not allowed_host(hostname)
    ):
        raise RecordingError("The recording provider returned an invalid media location.")
    return url


def _smallest_media_url(url: str) -> str:
    # Smallest's documented endpoint returns a presigned S3 URL. Restricting
    # the second hop to HTTPS AWS object storage prevents the provider response
    # from becoming an SSRF primitive. Redirects are rejected separately.
    return _validate_https_url(
        url,
        allowed_host=_is_aws_s3_host,
    )


def _is_aws_s3_host(hostname: str) -> bool:
    """Accept documented S3 data endpoints without trusting every AWS API.

    AWS service hostnames share ``amazonaws.com``. Merely finding an ``s3``
    label anywhere in that suffix would also admit shapes such as
    ``s3.execute-api.<region>.amazonaws.com``. Match the S3 service portion at
    the end instead, while allowing path-style, virtual-hosted, dual-stack,
    FIPS, accelerated, and access-point object endpoints.
    """
    aws_suffix = ".amazonaws.com"
    if not hostname.endswith(aws_suffix):
        return False
    endpoint_labels = hostname[: -len(aws_suffix)].split(".")
    if not endpoint_labels or not all(_is_dns_label(label) for label in endpoint_labels):
        return False

    prefix_labels: list[str] | None = None
    if endpoint_labels[-1] == "s3":
        # s3.amazonaws.com or <bucket>.s3.amazonaws.com
        prefix_labels = endpoint_labels[:-1]
    elif (
        len(endpoint_labels) >= 2
        and endpoint_labels[-2] == "s3"
        and _is_aws_region(endpoint_labels[-1])
    ):
        # s3.<region>.amazonaws.com or <bucket>.s3.<region>.amazonaws.com
        prefix_labels = endpoint_labels[:-2]
    elif (
        len(endpoint_labels) >= 3
        and endpoint_labels[-3:-1] == ["s3", "dualstack"]
        and _is_aws_region(endpoint_labels[-1])
    ):
        prefix_labels = endpoint_labels[:-3]
    elif endpoint_labels[-1].startswith("s3-") and _is_aws_region(
        endpoint_labels[-1][len("s3-") :]
    ):
        # Legacy regional form: s3-<region>.amazonaws.com.
        prefix_labels = endpoint_labels[:-1]
    elif (
        len(endpoint_labels) >= 2
        and endpoint_labels[-2] == "s3-fips"
        and _is_aws_region(endpoint_labels[-1])
    ):
        prefix_labels = endpoint_labels[:-2]
    elif endpoint_labels[-1].startswith("s3-fips-") and _is_aws_region(
        endpoint_labels[-1][len("s3-fips-") :]
    ):
        prefix_labels = endpoint_labels[:-1]
    elif endpoint_labels[-1] == "s3-accelerate":
        prefix_labels = endpoint_labels[:-1]
        if not prefix_labels:
            return False
    elif endpoint_labels[-2:] == ["s3-accelerate", "dualstack"]:
        prefix_labels = endpoint_labels[:-2]
        if not prefix_labels:
            return False
    elif (
        len(endpoint_labels) >= 2
        and endpoint_labels[-2] == "s3-accesspoint"
        and _is_aws_region(endpoint_labels[-1])
    ):
        prefix_labels = endpoint_labels[:-2]
        if not prefix_labels:
            return False
    elif (
        len(endpoint_labels) >= 3
        and endpoint_labels[-3:-1] == ["s3-accesspoint", "dualstack"]
        and _is_aws_region(endpoint_labels[-1])
    ):
        prefix_labels = endpoint_labels[:-3]
        if not prefix_labels:
            return False

    # Empty is valid for the path-style S3 service endpoint. Any non-empty
    # prefix was already constrained to ordinary DNS labels above.
    return prefix_labels is not None


def _is_dns_label(label: str) -> bool:
    return bool(1 <= len(label) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label))


def _is_aws_region(label: str) -> bool:
    # Covers commercial, GovCloud, ISO, and future region families while still
    # rejecting service labels such as ``execute-api`` and ``sts``.
    return bool(re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", label))


def _twilio_media_url(url: str) -> str:
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise RecordingError("Twilio recording access is not configured.", status_code=503)
    validated = _validate_https_url(url, allowed_host=lambda hostname: hostname == "api.twilio.com")
    parsed = urlsplit(validated)
    match = _TWILIO_RECORDING_PATH.fullmatch(parsed.path)
    if not match or match.group(1) != settings.twilio_account_sid or parsed.query:
        raise RecordingError("The recording provider returned an invalid media location.")

    # Twilio documents that appending .mp3 to this exact Recording resource
    # returns media protected by HTTP Basic authentication. Never trust a
    # callback-supplied host, account, query, or redirect target.
    media_path = re.sub(r"\.(?:mp3|wav)$", "", parsed.path) + ".mp3"
    return f"https://api.twilio.com{media_path}"


def _sniff_audio(content: bytes) -> tuple[str, str] | None:
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if len(content) >= 4 and content[:4] == b"OggS":
        return "audio/ogg", "ogg"
    if len(content) >= 8 and content[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    if content[:3] == b"ID3" or (
        len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg", "mp3"
    return None


async def _download_audio(
    url: str,
    *,
    auth: httpx.Auth | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RecordingAudio:
    try:
        async with httpx.AsyncClient(
            timeout=RECORDING_TIMEOUT,
            follow_redirects=False,
            transport=transport,
            auth=auth,
            headers={"Accept": "audio/mpeg, audio/wav, audio/ogg, audio/mp4"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise RecordingError("The recording provider returned an unsafe redirect.")
                if response.status_code == 404:
                    raise RecordingError("Recording is not available.", status_code=404)
                if response.status_code in {401, 403}:
                    raise RecordingError("The recording provider rejected server credentials.")
                if response.is_error:
                    raise RecordingError("The recording provider could not return this audio.")

                declared_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if declared_type not in _ALLOWED_RECORDING_CONTENT_TYPES:
                    raise RecordingError(
                        "The recording provider returned an unexpected media type."
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_RECORDING_BYTES:
                            raise RecordingError("The recording exceeds the secure playback limit.")
                    except ValueError:
                        pass

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RECORDING_BYTES:
                        raise RecordingError("The recording exceeds the secure playback limit.")
                    content.extend(chunk)
    except RecordingError:
        raise
    except httpx.TimeoutException as exc:
        raise RecordingError("The recording provider timed out.", status_code=504) from exc
    except httpx.HTTPError as exc:
        raise RecordingError("The recording provider could not be reached.") from exc

    resolved_type = _sniff_audio(bytes(content))
    if resolved_type is None:
        raise RecordingError("The recording provider returned invalid audio.")
    content_type, extension = resolved_type
    expected_type = _ALLOWED_RECORDING_CONTENT_TYPES[declared_type]
    if expected_type is not None and expected_type != content_type:
        raise RecordingError("The recording provider returned inconsistent audio content.")
    return RecordingAudio(content=bytes(content), content_type=content_type, extension=extension)


async def fetch_call_recording(
    call: Call,
    *,
    smallest_client: SmallestAIClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RecordingAudio:
    """Retrieve one recording without exposing provider locators or secrets."""
    if call.provider == "smallest":
        if not call.provider_call_sid:
            raise RecordingError("Recording is not available.", status_code=404)
        client = smallest_client or get_smallest_client()
        try:
            download_url = await client.get_recording_download_url(call_id=call.provider_call_sid)
        except SmallestAIError as exc:
            status_code = exc.status_code if exc.status_code in {404, 503, 504} else 502
            message = {
                404: "Recording is not available.",
                503: "Recording access is not configured.",
                504: "The recording provider timed out.",
            }.get(status_code, "The recording provider could not return this audio.")
            raise RecordingError(message, status_code=status_code) from exc
        return await _download_audio(
            _smallest_media_url(download_url),
            transport=transport,
        )

    if call.provider == "twilio":
        if not call.provider_recording_url:
            raise RecordingError("Recording is not available.", status_code=404)
        media_url = _twilio_media_url(call.provider_recording_url)
        return await _download_audio(
            media_url,
            auth=httpx.BasicAuth(settings.twilio_account_sid, settings.twilio_auth_token),
            transport=transport,
        )

    raise RecordingError(
        "This call provider does not support secure recording playback.", status_code=422
    )
