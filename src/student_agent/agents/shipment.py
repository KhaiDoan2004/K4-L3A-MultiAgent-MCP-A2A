"""Shipment agent: delivery dates, seller handoff limits and who caused a delay."""

from __future__ import annotations

from typing import Any

from ..a2a import Finding, ScopedGateway
from ..scoping import Timeline, parse_ts, scope_rows

ACTOR = "shipment_agent"
TOOLS = frozenset({"get_shipment_summary"})

LATE_EVENTS = {"delivered_late", "delivery_delayed", "late_delivery"}
SELLER_ACTORS = {"seller", "merchant"}
LOGISTICS_ACTORS = {"logistics_provider", "carrier", "logistics"}


async def run(order_id: str, timeline: Timeline, gateway: ScopedGateway) -> Finding:
    facts: dict[str, Any] = {}

    evidence = await gateway.call("get_shipment_summary", order_id=order_id)
    payload = (evidence or {}).get("data") or {}
    if not isinstance(payload, dict):
        payload = {}

    rows = [row for row in payload.get("events") or [] if isinstance(row, dict)]
    events, stale_events = scope_rows(rows, "event_at", timeline.in_shipment_window)
    late_events = [row for row in events if row.get("event_type") in LATE_EVENTS]

    facts["has_shipment_evidence"] = evidence is not None
    facts["status"] = payload.get("order_status")
    facts["late_events"] = late_events
    facts["late_actor"] = _late_actor(late_events)
    facts["shipping_limits"] = [
        moment
        for moment in (
            parse_ts(row.get("shipping_limit_at")) for row in payload.get("shipping_limits") or []
            if isinstance(row, dict)
        )
        if moment is not None and timeline.in_item_window(moment)
    ]
    facts["is_late"] = timeline.is_late()

    conflicts: list[dict[str, Any]] = []
    if stale_events:
        conflicts.append(
            {
                "field": "shipment_summary.events",
                "sources": ["get_shipment_summary.in_scope", "get_shipment_summary.out_of_scope"],
                "selected_source": "get_shipment_summary.in_scope",
                "resolution_code": "PREFER_ORDER_TIMELINE_SCOPE",
            }
        )
    if any(row.get("event_type") in LATE_EVENTS for row in stale_events) and (
        timeline.delivered is None
    ):
        conflicts.append(
            {
                "field": "order_status_vs_delivery_event",
                "sources": ["get_order.order_status", "get_shipment_summary.events"],
                "selected_source": "get_order.order_status",
                "resolution_code": "AUTHORITATIVE_ORDER_ROW",
            }
        )
    facts["conflicts"] = conflicts

    return Finding(
        actor=ACTOR, facts=facts, evidence=dict(gateway.evidence), warnings=list(gateway.warnings)
    )


def _late_actor(events: list[dict[str, Any]]) -> str | None:
    for row in events:
        actor = str(row.get("actor") or "").lower()
        if actor in SELLER_ACTORS:
            return "seller"
        if actor in LOGISTICS_ACTORS:
            return "logistics_provider"
    return None
