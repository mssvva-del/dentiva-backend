"""Stripe webhook handler (Platform Iter 1, Phase D).

Keeps our billing rows in sync with Stripe and drives the failed-payment →
grace → suspend lifecycle.

SECURITY: signature verified against STRIPE_WEBHOOK_SECRET. Unset → allowed in
dev (logged), REJECTED in production (an unverified billing webhook could flip a
practice's payment status). Unknown events are acked + ignored so Stripe stops
retrying. Handlers are idempotent (Stripe redelivers).

Events handled:
  checkout.session.completed        → record customer, activate subscription
  invoice.paid                      → store invoice paid, (re)activate
  invoice.payment_failed            → past_due (grace; Stripe retries)
  customer.subscription.deleted     → suspend (Stripe gave up after retries)
  customer.subscription.updated     → sync status
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

import app.db as _app_db
from app.config import get_settings
from app.db import set_tenant
from app.models.invoice import Invoice
from app.models.practice import Practice
from app.models.processed_webhook_event import ProcessedWebhookEvent
from app.models.subscription import Subscription
from app.observability.alerts import record_alert
from app.services.billing_service import (
    create_or_update_subscription,
    mark_past_due,
    reactivate_practice,
    suspend_practice,
)
from app.webhooks.stripe_verify import StripeVerificationError, verify

logger = logging.getLogger("dentiva.webhooks.stripe")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify(raw_body: bytes, sig_header: str | None) -> None:
    settings = get_settings()
    secret = settings.stripe_webhook_secret
    if not secret:
        if settings.environment == "production":
            logger.error("STRIPE_WEBHOOK_SECRET unset in production — rejecting webhook.")
            raise HTTPException(status_code=401, detail="Webhook not configured.")
        logger.warning("STRIPE_WEBHOOK_SECRET empty — skipping signature check (dev).")
        return
    try:
        verify(secret=secret, raw_body=raw_body, signature_header=sig_header)
    except StripeVerificationError as exc:
        logger.warning("stripe webhook signature rejected: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid webhook signature.") from exc


async def _resolve_practice(session, obj: dict) -> Practice | None:
    """Find the practice an event object belongs to.

    Tries (in order): our explicit metadata.practice_id (set when we create the
    checkout session), the Stripe customer id, then the Stripe subscription id.
    """
    meta = obj.get("metadata") or {}
    pid = meta.get("practice_id")
    if pid:
        try:
            p = (
                await session.execute(select(Practice).where(Practice.id == uuid.UUID(pid)))
            ).scalar_one_or_none()
            if p:
                return p
        except ValueError:
            pass

    customer = obj.get("customer")
    if customer:
        p = (
            await session.execute(
                select(Practice).where(Practice.stripe_customer_id == customer)
            )
        ).scalar_one_or_none()
        if p:
            return p

    sub_id = obj.get("subscription")
    if not sub_id and obj.get("object") == "subscription":
        sub_id = obj.get("id")
    if sub_id:
        sub = (
            await session.execute(
                select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
            )
        ).scalar_one_or_none()
        if sub:
            return (
                await session.execute(select(Practice).where(Practice.id == sub.practice_id))
            ).scalar_one_or_none()
    return None


def _epoch(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return None


async def _handle_checkout_completed(session, obj: dict) -> str:
    practice = await _resolve_practice(session, obj)
    if practice is None:
        return "no_practice"
    customer = obj.get("customer")
    if customer:
        practice.stripe_customer_id = customer
    await set_tenant(session, practice.id)
    meta = obj.get("metadata") or {}
    plan = meta.get("plan", "after_hours")
    cycle = meta.get("billing_cycle", "monthly")
    await create_or_update_subscription(
        session, practice, plan_key=plan, billing_cycle=cycle, status="active",
        stripe_subscription_id=obj.get("subscription"),
    )
    return "subscription_active"


async def _upsert_invoice(
    session, practice_id, *, stripe_id: str | None, amount_cents: int, status_: str,
    paid_at: datetime | None, period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> None:
    """Idempotently record an invoice. Stripe REDELIVERS webhooks, so we upsert on
    stripe_invoice_id (partial-unique) instead of blind-inserting, which used to
    create duplicate invoice rows on every retry."""
    mutable = {
        "amount_cents": amount_cents,
        # Never DOWNGRADE an admin-refunded invoice back to 'paid' on a redelivered
        # invoice.paid — that would re-arm the refund button after money already
        # moved. Refund states are terminal for this upsert.
        "status": text(
            "CASE WHEN invoices.status IN ('refunded','partially_refunded') "
            "THEN invoices.status ELSE excluded.status END"
        ),
        "paid_at": paid_at,
        "period_start": period_start, "period_end": period_end,
        "updated_at": datetime.now(UTC),
    }
    if not stripe_id:
        # No Stripe id to dedup on (rare/edge) — plain insert.
        session.add(Invoice(
            id=uuid.uuid4(), practice_id=practice_id, stripe_invoice_id=None,
            amount_cents=amount_cents, status=status_, paid_at=paid_at,
            period_start=period_start, period_end=period_end,
        ))
        return
    stmt = (
        pg_insert(Invoice)
        .values(
            id=uuid.uuid4(), practice_id=practice_id, stripe_invoice_id=stripe_id,
            amount_cents=amount_cents, status=status_, paid_at=paid_at,
            period_start=period_start, period_end=period_end,
        )
        .on_conflict_do_update(
            index_elements=["stripe_invoice_id"],
            index_where=text("stripe_invoice_id IS NOT NULL"),
            set_=mutable,
        )
    )
    await session.execute(stmt)


async def _handle_invoice_paid(session, obj: dict) -> str:
    practice = await _resolve_practice(session, obj)
    if practice is None:
        return "no_practice"
    await set_tenant(session, practice.id)
    await _upsert_invoice(
        session, practice.id,
        stripe_id=obj.get("id"),
        amount_cents=int(obj.get("amount_paid") or obj.get("amount_due") or 0),
        status_="paid",
        paid_at=datetime.now(UTC),
        period_start=_epoch(obj.get("period_start")),
        period_end=_epoch(obj.get("period_end")),
    )
    # A successful payment clears any suspension/past-due.
    await reactivate_practice(session, practice)
    return "invoice_paid"


async def _handle_payment_failed(session, obj: dict) -> str:
    practice = await _resolve_practice(session, obj)
    if practice is None:
        return "no_practice"
    await set_tenant(session, practice.id)
    await _upsert_invoice(
        session, practice.id,
        stripe_id=obj.get("id"),
        amount_cents=int(obj.get("amount_due") or 0),
        status_="open",
        paid_at=None,
    )
    # Grace period: Stripe will retry. We mark past_due but do NOT suspend yet —
    # suspension happens on customer.subscription.deleted (Stripe gave up).
    await mark_past_due(session, practice.id)
    return "past_due"


async def _handle_subscription_deleted(session, obj: dict) -> str:
    practice = await _resolve_practice(session, obj)
    if practice is None:
        return "no_practice"
    await set_tenant(session, practice.id)
    await suspend_practice(session, practice)
    return "suspended"


async def _handle_subscription_updated(session, obj: dict) -> str:
    practice = await _resolve_practice(session, obj)
    if practice is None:
        return "no_practice"
    await set_tenant(session, practice.id)
    sub = (
        await session.execute(
            select(Subscription).where(Subscription.practice_id == practice.id)
        )
    ).scalar_one_or_none()
    if sub is None:
        return "no_subscription"
    # Ordering guard: Stripe redelivers failed webhooks, so an OLD updated-event can
    # arrive AFTER a newer one (e.g. cancel→resume, then the stale 'cancel' retry).
    # Applying it would roll status/cancel_at_period_end back — skip stale events.
    event_ts = obj.get("_event_created")
    if (
        event_ts is not None
        and sub.last_stripe_event_ts is not None
        and int(event_ts) < sub.last_stripe_event_ts
    ):
        return "stale_event_skipped"
    if event_ts is not None:
        sub.last_stripe_event_ts = int(event_ts)
    # Map Stripe status → ours where they differ (canceled→cancelled, unpaid→past_due).
    stripe_status = obj.get("status", "")
    mapping = {
        "active": "active", "trialing": "trialing", "past_due": "past_due",
        "canceled": "cancelled", "unpaid": "past_due",
    }
    sub.status = mapping.get(stripe_status, sub.status)
    if start := _epoch(obj.get("current_period_start")):
        sub.current_period_start = start
    if end := _epoch(obj.get("current_period_end")):
        sub.current_period_end = end
    # Keep the pending-cancel flag in sync (set/cleared in the Stripe dashboard or
    # by our admin cancel/resume endpoints). Only when the key is present — its
    # absence must not clear a scheduled cancellation.
    if "cancel_at_period_end" in obj:
        sub.cancel_at_period_end = bool(obj["cancel_at_period_end"])
    return f"synced:{sub.status}"


_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "invoice.paid": _handle_invoice_paid,
    "invoice.payment_failed": _handle_payment_failed,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "customer.subscription.updated": _handle_subscription_updated,
}


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
) -> dict:
    raw_body = await request.body()
    _verify(raw_body, stripe_signature)

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc

    event_type = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    # Smuggle the event's creation time to handlers that need ordering protection
    # (underscore key — cannot collide with Stripe object fields).
    if event.get("created") is not None:
        obj["_event_created"] = event["created"]
    handler = _HANDLERS.get(event_type)
    if handler is None:
        logger.info("stripe webhook: ignoring unmodeled event '%s'", event_type)
        return {"status": "ignored", "type": event_type}

    async with _app_db.async_session_factory() as session:
        try:
            # Dedup EVERY event type by Stripe's event id, claimed in the SAME
            # transaction as the handler: a redelivered checkout/invoice/etc. is a
            # no-op (so a stale checkout.session.completed can't reactivate a
            # suspended practice), yet a handler that errors rolls back the claim
            # too, so a genuine retry still runs.
            event_id = event.get("id")
            if event_id:
                claim = await session.execute(
                    pg_insert(ProcessedWebhookEvent)
                    .values(id=uuid.uuid4(), source="stripe", event_id=event_id)
                    .on_conflict_do_nothing(index_elements=["source", "event_id"])
                )
                if claim.rowcount == 0:
                    logger.info("stripe webhook: duplicate event %s ignored", event_id)
                    return {"status": "duplicate", "type": event_type}
            result = await handler(session, obj)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("stripe webhook handler failed for '%s'", event_type)
            # Repeated Stripe 500s = money events silently not applied — page us.
            record_alert("stripe_handler_failed", f"type={event_type}")
            raise HTTPException(status_code=500, detail="Webhook processing failed.") from None

    logger.info("stripe webhook '%s' → %s", event_type, result)
    return {"status": "ok", "type": event_type, "result": result}
