"""Payment agent: captures, reconciliation and the refund lifecycle."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from ..a2a import Finding, ScopedGateway
from ..scoping import Timeline, dedupe, money, parse_ts, scope_payments, scope_rows

ACTOR = "payment_agent"
TOOLS = frozenset({"get_payment_timeline", "get_refund_timeline"})

# A refund that never settled is reported through the status of its request.
FAILED_STATUSES = {"failed", "error", "rejected", "reversed"}
PENDING_STATUSES = {"pending", "open", "processing", "requested", "in_progress"}


async def run(order_id: str, timeline: Timeline, gateway: ScopedGateway) -> Finding:
    facts: dict[str, Any] = {}

    payment_evidence = await gateway.call("get_payment_timeline", order_id=order_id)
    payload = (payment_evidence or {}).get("data") or {}
    if not isinstance(payload, dict):
        payload = {}
    events = [row for row in payload.get("events") or [] if isinstance(row, dict)]
    rows = [row for row in payload.get("payments") or [] if isinstance(row, dict)]

    scoped_events, stale_events = scope_rows(events, "event_at", timeline.in_payment_window)
    captures = [row for row in scoped_events if row.get("event_type") == "captured"]
    capture_amounts = {
        amount
        for amount in (money(row.get("amount_brl")) for row in captures)
        if amount is not None
    }
    payments, stale_payments = scope_payments(rows, capture_amounts)
    if not payments and rows:
        payments, stale_payments = dedupe(rows), []
        gateway.warnings.append("get_payment_timeline: no capture matched the approval window")

    facts["payment_rows"] = payments
    facts["payment_total"] = sum(
        (money(row.get("payment_value")) or Decimal("0") for row in payments), Decimal("0")
    )
    facts["payment_references"] = _unique(row.get("payment_sequential") for row in payments)
    facts["payment_types"] = _unique(row.get("payment_type") for row in payments)
    facts["captured_total"] = sum(capture_amounts, Decimal("0"))
    facts["capture_count"] = len(captures)
    facts["has_capture"] = bool(captures)
    facts["repeated_amount"] = _repeated_amount(payments)
    facts["has_mismatch_event"] = any(
        row.get("event_type") == "reconciliation_mismatch" for row in scoped_events
    )

    refund_evidence = await gateway.call("get_refund_timeline", order_id=order_id)
    refund_payload = (refund_evidence or {}).get("data") or {}
    if not isinstance(refund_payload, dict):
        refund_payload = {}
    refund_rows = [row for row in refund_payload.get("events") or [] if isinstance(row, dict)]
    refunds, stale_refunds = scope_rows(refund_rows, "event_at", timeline.in_refund_window)
    # A refund belongs to this order only if it moves money this order actually paid.
    # Injected refunds carry the amount of the distractor payment, not of the real one.
    if capture_amounts:
        matched = [row for row in refunds if _refunded_amount_was_paid(row, capture_amounts)]
        stale_refunds += [row for row in refunds if row not in matched]
        refunds = matched
    facts["refund_events"] = refunds
    facts["refund_status"] = _refund_status(refunds)
    facts["refund_amount"] = _latest_refund_amount(refunds)
    facts["has_refund_evidence"] = refund_evidence is not None

    conflicts: list[dict[str, Any]] = []
    if stale_payments or stale_events:
        conflicts.append(
            {
                "field": "payment_timeline.events",
                "sources": ["get_payment_timeline.in_scope", "get_payment_timeline.out_of_scope"],
                "selected_source": "get_payment_timeline.in_scope",
                "resolution_code": "PREFER_PAYMENT_APPROVAL_WINDOW",
            }
        )
    if stale_refunds:
        conflicts.append(
            {
                "field": "refund_timeline.events",
                "sources": ["get_refund_timeline.in_scope", "get_refund_timeline.out_of_scope"],
                "selected_source": "get_refund_timeline.in_scope",
                "resolution_code": "PREFER_ORDER_TIMELINE_SCOPE",
            }
        )
    facts["conflicts"] = conflicts

    return Finding(
        actor=ACTOR, facts=facts, evidence=dict(gateway.evidence), warnings=list(gateway.warnings)
    )


def _unique(values) -> list[str]:
    seen: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen.append(value)
    return seen


def _repeated_amount(payments: list[dict[str, Any]]) -> Decimal | None:
    """The amount that two distinct payment rows both charge, if any."""
    amounts = [money(row.get("payment_value")) for row in payments]
    for amount, count in Counter(value for value in amounts if value is not None).most_common(1):
        if count >= 2:
            return amount
    return None


def _refunded_amount_was_paid(event: dict[str, Any], paid_amounts: set[Decimal]) -> bool:
    amount = money(event.get("amount_brl"))
    if amount is None:
        return True  # nothing to contradict
    return amount in paid_amounts or amount == sum(paid_amounts, Decimal("0"))


def _refund_status(events: list[dict[str, Any]]) -> str | None:
    statuses = {str(row.get("status") or "").lower() for row in events}
    if statuses & FAILED_STATUSES:
        return "failed"
    if statuses & PENDING_STATUSES:
        return "pending"
    if statuses:
        return "settled"
    return None


def _latest_refund_amount(events: list[dict[str, Any]]) -> Decimal | None:
    ordered = sorted(events, key=lambda row: parse_ts(row.get("event_at")) or _MIN)
    for row in reversed(ordered):
        amount = money(row.get("amount_brl"))
        if amount is not None:
            return amount
    return None


_MIN = parse_ts("1970-01-01T00:00:00+00:00")
