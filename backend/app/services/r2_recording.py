"""Server-owned recording identity, bounded private retrieval and provider requests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from livekit import api

from app.core.config import settings

NOTICE_VERSION = "browser-recording-90d-v1"
NOTICE = (
    "Record this conversation, including your voice and the AI's voice, in private "
    "Cloudflare storage outside the UAE for up to 90 days. Authorized workspace staff "
    "can play it back. Stop recording or end the call to stop capture."
)


def object_key(tenant_id: UUID, call_id: UUID) -> str:
    return f"recordings/{UUID(str(tenant_id))}/{UUID(str(call_id))}/audio.ogg"


def configured() -> bool:
    return bool(
        settings.recording_enabled
        and settings.recording_s3_access_key_id
        and settings.recording_s3_secret_access_key
        and settings.recording_s3_bucket
        and settings.recording_s3_endpoint.startswith("https://")
    )


def storage_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.recording_s3_endpoint,
        aws_access_key_id=settings.recording_s3_access_key_id,
        aws_secret_access_key=settings.recording_s3_secret_access_key,
        region_name=settings.recording_s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 1},
        ),
    )


def egress_request(tenant_id: UUID, call_id: UUID, room_name: str):
    return api.StartEgressRequest(
        room_name=room_name,
        template=api.TemplateSource(audio_only=True),
        outputs=[
            api.Output(
                file=api.FileOutput(
                    file_type=api.EncodedFileType.OGG,
                    filepath=object_key(tenant_id, call_id),
                    disable_manifest=True,
                )
            )
        ],
        storage=api.StorageConfig(
            s3=api.S3Upload(
                access_key=settings.recording_s3_access_key_id,
                secret=settings.recording_s3_secret_access_key,
                endpoint=settings.recording_s3_endpoint,
                region=settings.recording_s3_region,
                bucket=settings.recording_s3_bucket,
                force_path_style=True,
            )
        ),
        webhooks=[
            api.WebhookConfig(
                url=settings.base_url.rstrip("/") + "/api/v1/call-recordings/webhook",
                signing_key=settings.livekit_api_key,
            )
        ],
    )


def recording_state(metadata: object) -> dict:
    if not isinstance(metadata, dict):
        return {}
    item = metadata.get("private_recording")
    return dict(item) if isinstance(item, dict) else {}


def matches_request(info, tenant_id: UUID, call_id: UUID) -> bool:
    """Recover a lost start response only from the exact server-owned destination."""
    return bool(
        info.room_name == f"vav-browser-{call_id}"
        and info.HasField("egress")
        and len(info.egress.outputs) == 1
        and info.egress.outputs[0].HasField("file")
        and info.egress.outputs[0].file.filepath == object_key(tenant_id, call_id)
    )


def expires_at(state: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(state["expires_at"])
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    except (KeyError, ValueError, TypeError):
        return None


def available(state: dict, now: datetime | None = None) -> bool:
    expiry = expires_at(state)
    return bool(state.get("state") == "ready" and expiry and expiry > (now or datetime.now(UTC)))


def apply_provider_state(state: dict, info) -> dict:
    # Never let a delayed active event regress a terminal/ready recording.
    if state.get("state") in {"ready", "failed", "expired"}:
        return state
    status = info.status
    if status == api.EgressStatus.EGRESS_COMPLETE:
        return {**state, "state": "ready"}
    if status in (
        api.EgressStatus.EGRESS_FAILED,
        api.EgressStatus.EGRESS_ABORTED,
        api.EgressStatus.EGRESS_LIMIT_REACHED,
    ):
        return {**state, "state": "failed"}
    if status == api.EgressStatus.EGRESS_ENDING:
        return {**state, "state": "processing"}
    if status == api.EgressStatus.EGRESS_ACTIVE and state.get("state") != "processing":
        return {**state, "state": "recording"}
    return state


async def fetch_audio(call):
    from app.services.recordings import RecordingAudio, RecordingError

    state = recording_state(call.call_metadata)
    if not available(state):
        raise RecordingError("Recording is unavailable, processing, or expired.", status_code=404)
    key = object_key(call.tenant_id, call.id)

    def download():
        response = storage_client().get_object(Bucket=settings.recording_s3_bucket, Key=key)
        body = response["Body"]
        try:
            if int(response.get("ContentLength", 0)) > 64 * 1024 * 1024:
                raise ValueError("Oversized recording")
            content = body.read(64 * 1024 * 1024 + 1)
        finally:
            body.close()
        if len(content) > 64 * 1024 * 1024 or not content.startswith(b"OggS"):
            raise ValueError("Invalid recording")
        return content

    try:
        content = await asyncio.wait_for(asyncio.to_thread(download), timeout=30)
    except (ClientError, BotoCoreError, TimeoutError, ValueError) as exc:
        raise RecordingError("Private recording could not be retrieved.", status_code=502) from exc
    # Expiry may occur while downloading; deny release even before R2's deletion runs.
    if not available(state):
        raise RecordingError("Recording has expired.", status_code=410)
    return RecordingAudio(content=content, content_type="audio/ogg", extension="ogg")
