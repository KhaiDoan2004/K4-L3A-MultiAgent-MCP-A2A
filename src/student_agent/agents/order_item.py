"""Order/item agent: the authoritative order row, its line items and sellers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..a2a import Finding, ScopedGateway
from ..scoping import Timeline, dedupe, money, parse_ts, scope_rows

ACTOR = "order_item_agent"
TOOLS = frozenset({"get_order", "get_order_items"})


async def run(order_id: str, gateway: ScopedGateway) -> Finding:
    facts: dict[str, Any] = {"order_id": order_id}

    order_evidence = await gateway.call("get_order", order_id=order_id)
    order = (order_evidence or {}).get("data") or {}
    if not isinstance(order, dict):
        order = {}
    timeline = Timeline.from_order(order)
    facts["order_status"] = order.get("order_status")
    facts["timeline"] = timeline
    facts["has_order"] = bool(order)

    items_evidence = await gateway.call("get_order_items", order_id=order_id)
    rows = (items_evidence or {}).get("data") or []
    if not isinstance(rows, list):
        rows = []
    rows = dedupe([row for row in rows if isinstance(row, dict)])
    inside, outside = scope_rows(rows, "shipping_limit_date", timeline.in_item_window)
    if not inside and rows:
        # Nothing matched the window; fall back to the earliest limit rather than
        # dropping every item and reporting an empty order.
        inside = sorted(rows, key=lambda row: str(row.get("shipping_limit_date")))[:1]
        outside = [row for row in rows if row not in inside]
        gateway.warnings.append("get_order_items: no row inside the order window")

    facts["item_ids"] = _unique(row.get("order_item_id") for row in inside)
    facts["seller_ids"] = _unique(row.get("seller_id") for row in inside)
    facts["price_total"] = _total(inside, "price")
    facts["freight_total"] = _total(inside, "freight_value")
    facts["order_total"] = facts["price_total"] + facts["freight_total"]
    facts["shipping_limits"] = [
        moment
        for moment in (parse_ts(row.get("shipping_limit_date")) for row in inside)
        if moment is not None
    ]
    facts["discarded_item_rows"] = len(outside)

    conflicts: list[dict[str, Any]] = []
    if outside:
        conflicts.append(
            {
                "field": "order_items.shipping_limit_date",
                "sources": ["get_order_items.in_scope", "get_order_items.out_of_scope"],
                "selected_source": "get_order_items.in_scope",
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


def _total(rows: list[dict[str, Any]], field: str) -> Decimal:
    total = Decimal("0")
    for row in rows:
        value = money(row.get(field))
        if value is not None:
            total += value
    return total
