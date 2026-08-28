from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.middleware.tenant import CurrentUser, get_current_user, require_role
from app.models.agent import Agent
from app.models.commerce import CommerceAction, CommerceSession
from app.schemas.commerce import (
    CommerceCartItemRequest,
    CommerceCheckoutRequest,
    CommerceConfirmationRequest,
    CommerceProductRequest,
    CommerceProviderStatus,
    CommerceSearchRequest,
    CommerceSessionCreate,
    CommerceSessionResponse,
    CommerceSubmitRequest,
)
from app.services.audit import record_audit_event
from app.services.fepy_browser import FepyBrowserError, fepy_browser
from app.services.integration_security import decrypt_integration_config, encrypt_integration_config

router = APIRouter(prefix="/commerce", tags=["Browser Commerce"])


def _session_response(session: CommerceSession) -> CommerceSessionResponse:
    return CommerceSessionResponse.model_validate(session)


def _context(session: CommerceSession) -> dict:
    return (
        decrypt_integration_config(session.encrypted_context) if session.encrypted_context else {}
    )


def _store_context(session: CommerceSession, value: dict) -> None:
    session.encrypted_context = encrypt_integration_config(value)


async def _load_session(db: AsyncSession, tenant_id: UUID, session_id: UUID) -> CommerceSession:
    result = await db.execute(
        select(CommerceSession)
        .options(selectinload(CommerceSession.actions))
        .where(CommerceSession.id == session_id, CommerceSession.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Commerce session not found")
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC) and session.status not in {"completed", "expired"}:
        session.status = "expired"
    return session


async def _action(
    db: AsyncSession,
    session: CommerceSession,
    action_type: str,
    idempotency_key: str | None,
    request_summary: dict,
):
    key = (idempotency_key or str(uuid4())).strip()
    existing = await db.execute(
        select(CommerceAction).where(
            CommerceAction.tenant_id == session.tenant_id,
            CommerceAction.idempotency_key == key,
        )
    )
    previous = existing.scalar_one_or_none()
    if previous:
        if previous.session_id != session.id or previous.action_type != action_type:
            raise HTTPException(
                status_code=409, detail="Idempotency key was used for another action"
            )
        return previous, False
    item = CommerceAction(
        tenant_id=session.tenant_id,
        session_id=session.id,
        action_type=action_type,
        idempotency_key=key,
        request_summary=request_summary,
        status="running",
    )
    db.add(item)
    await db.flush()
    return item, True


def _finish(action: CommerceAction, started: float, result: dict) -> None:
    action.status = "completed"
    action.result_summary = result
    action.duration_ms = round((monotonic() - started) * 1000)


def _fail(action: CommerceAction, started: float, exc: Exception) -> None:
    action.status = "failed"
    action.error_message = str(exc)[:500]
    action.duration_ms = round((monotonic() - started) * 1000)


def _verified_cart(snapshot: dict) -> bool:
    try:
        item_count = int(snapshot.get("item_count") or 0)
        total = Decimal(str(snapshot.get("total_including_vat") or "0").replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return snapshot.get("verified") is True and item_count > 0 and total > 0


async def _persist_browser_failure(
    db: AsyncSession,
    session: CommerceSession,
    action: CommerceAction,
    started: float,
    exc: FepyBrowserError,
) -> None:
    _fail(action, started, exc)
    session.last_error = str(exc)
    await db.flush()
    await db.commit()


def _guard_replayed_action(action: CommerceAction, execute: bool) -> None:
    if execute:
        return
    if action.status == "failed":
        raise HTTPException(
            status_code=502,
            detail=action.error_message or "The previous browser action failed",
        )
    if action.status == "running":
        raise HTTPException(status_code=409, detail="This browser action is still running")


@router.get("/status", response_model=CommerceProviderStatus)
async def provider_status(current_user: CurrentUser = Depends(get_current_user)):
    return CommerceProviderStatus(
        enabled=settings.fepy_commerce_enabled,
        order_submission_enabled=settings.fepy_allow_order_submission,
        shop_origin=settings.fepy_shop_origin,
        execution_mode="local_chromium" if settings.fepy_commerce_enabled else "disabled",
    )


@router.get("/sessions", response_model=list[CommerceSessionResponse])
async def list_sessions(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CommerceSession)
        .options(selectinload(CommerceSession.actions))
        .where(CommerceSession.tenant_id == current_user.tenant_id)
        .order_by(CommerceSession.created_at.desc())
        .limit(50)
    )
    return [_session_response(item) for item in result.scalars().unique().all()]


@router.post("/sessions", response_model=CommerceSessionResponse, status_code=201)
async def create_session(
    data: CommerceSessionCreate,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    if data.agent_id:
        found = await db.scalar(
            select(Agent.id).where(
                Agent.id == data.agent_id, Agent.tenant_id == current_user.tenant_id
            )
        )
        if not found:
            raise HTTPException(status_code=404, detail="Agent not found")
    session = CommerceSession(
        tenant_id=current_user.tenant_id,
        agent_id=data.agent_id,
        channel=data.channel,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    db.add(session)
    await db.flush()
    await db.refresh(session, attribute_names=["actions"])
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="commerce.session_created",
        resource_type="commerce_session",
        resource_id=str(session.id),
        details={"channel": data.channel},
    )
    return _session_response(session)


@router.post("/sessions/{session_id}/search", response_model=CommerceSessionResponse)
async def search_products(
    session_id: UUID,
    data: CommerceSearchRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    session = await _load_session(db, current_user.tenant_id, session_id)
    action, execute = await _action(db, session, "search", idempotency_key, {"query": data.query})
    _guard_replayed_action(action, execute)
    if execute:
        started = monotonic()
        try:
            result = await fepy_browser.search(data.query, data.limit)
            _finish(action, started, result)
            session.browser_checkpoint = {"stage": "search", "query": data.query}
            session.last_error = None
        except FepyBrowserError as exc:
            await _persist_browser_failure(db, session, action, started, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await db.flush()
    return _session_response(await _load_session(db, current_user.tenant_id, session.id))


@router.post("/sessions/{session_id}/product", response_model=CommerceSessionResponse)
async def inspect_product(
    session_id: UUID,
    data: CommerceProductRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    session = await _load_session(db, current_user.tenant_id, session_id)
    action, execute = await _action(
        db, session, "inspect_product", idempotency_key, {"product_path": data.product_path}
    )
    _guard_replayed_action(action, execute)
    if execute:
        started = monotonic()
        try:
            result = await fepy_browser.inspect_product(data.product_path)
            _finish(action, started, result)
            session.browser_checkpoint = {"stage": "product", "product_path": data.product_path}
        except FepyBrowserError as exc:
            await _persist_browser_failure(db, session, action, started, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await db.flush()
    return _session_response(await _load_session(db, current_user.tenant_id, session.id))


@router.post("/sessions/{session_id}/cart/items", response_model=CommerceSessionResponse)
async def add_cart_item(
    session_id: UUID,
    data: CommerceCartItemRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    session = await _load_session(db, current_user.tenant_id, session_id)
    if session.status not in {"active", "checkout_ready"}:
        raise HTTPException(status_code=409, detail="This cart can no longer be changed")
    action, execute = await _action(
        db,
        session,
        "add_cart_item",
        idempotency_key,
        {"product_path": data.product_path, "quantity": data.quantity},
    )
    _guard_replayed_action(action, execute)
    if execute:
        started = monotonic()
        context = _context(session)
        try:
            snapshot, storage = await fepy_browser.add_to_cart(
                data.product_path, data.quantity, context.get("browser_storage")
            )
            if not _verified_cart(snapshot):
                raise FepyBrowserError("FEPY cart contents could not be verified")
            context["browser_storage"] = storage
            _store_context(session, context)
            session.cart_snapshot = snapshot
            session.status = "active"
            session.cart_fingerprint = None
            session.confirmation_id = None
            session.confirmed_at = None
            _finish(action, started, snapshot)
        except FepyBrowserError as exc:
            await _persist_browser_failure(db, session, action, started, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await db.flush()
    return _session_response(await _load_session(db, current_user.tenant_id, session.id))


@router.post("/sessions/{session_id}/checkout", response_model=CommerceSessionResponse)
async def prepare_checkout(
    session_id: UUID,
    data: CommerceCheckoutRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    session = await _load_session(db, current_user.tenant_id, session_id)
    if not _verified_cart(session.cart_snapshot):
        raise HTTPException(status_code=409, detail="Add at least one verified product first")
    if data.payment_method == "hosted_card":
        session.checkout_url = f"{settings.fepy_shop_origin.rstrip('/')}/shop/cart"
    context = _context(session)
    context["customer"] = data.customer.model_dump()
    _store_context(session, context)
    session.payment_method = data.payment_method
    digest_source = json.dumps(session.cart_snapshot, sort_keys=True, separators=(",", ":"))
    session.cart_fingerprint = hashlib.sha256(digest_source.encode()).hexdigest()
    session.status = "awaiting_confirmation"
    session.confirmation_id = None
    session.confirmed_at = None
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="commerce.checkout_prepared",
        resource_type="commerce_session",
        resource_id=str(session.id),
        details={"payment_method": data.payment_method, "cart": session.cart_snapshot},
    )
    await db.flush()
    return _session_response(await _load_session(db, current_user.tenant_id, session.id))


@router.post("/sessions/{session_id}/confirm", response_model=CommerceSessionResponse)
async def confirm_order(
    session_id: UUID,
    data: CommerceConfirmationRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin", "member")),
    db: AsyncSession = Depends(get_db),
):
    session = await _load_session(db, current_user.tenant_id, session_id)
    if session.status != "awaiting_confirmation" or not session.cart_fingerprint:
        raise HTTPException(status_code=409, detail="Prepare and read back the checkout first")
    if " ".join(data.confirmation_text.upper().split()) != "CONFIRM ORDER":
        raise HTTPException(status_code=422, detail='Customer must explicitly say "Confirm order"')
    session.confirmation_id = secrets.token_urlsafe(24)
    session.confirmed_at = datetime.now(UTC)
    session.status = "confirmed"
    await record_audit_event(
        db,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        action="commerce.order_confirmed",
        resource_type="commerce_session",
        resource_id=str(session.id),
        details={"cart_fingerprint": session.cart_fingerprint},
    )
    await db.flush()
    return _session_response(await _load_session(db, current_user.tenant_id, session.id))


@router.post("/sessions/{session_id}/submit", response_model=CommerceSessionResponse)
async def submit_order(
    session_id: UUID,
    data: CommerceSubmitRequest,
    current_user: CurrentUser = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
):
    session = await _load_session(db, current_user.tenant_id, session_id)
    if session.status != "confirmed" or not secrets.compare_digest(
        session.confirmation_id or "", data.confirmation_id
    ):
        raise HTTPException(status_code=409, detail="A current explicit confirmation is required")
    if session.payment_method == "hosted_card":
        session.status = "checkout_ready"
        session.confirmation_id = None
        return _session_response(session)
    if not settings.fepy_allow_order_submission:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "COD/store-pickup submission is safety-locked until checkout selectors "
                "pass production acceptance tests"
            ),
        )
    raise HTTPException(status_code=501, detail="Order submission adapter is not yet activated")
