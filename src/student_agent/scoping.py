"""Separate the rows that belong to this order from injected look-alike rows.

Every case answers with two overlapping row sets: the order's own lifecycle and a
distractor copied from a different scenario, dated weeks or months away. The order
row from ``get_order`` is described by the server as authoritative, so its
timestamps anchor everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

# Distances measured against the anchors below. The injected rows in the sample
# cases sit at least three weeks away, so these windows have a wide safety margin.
ITEM_LIMIT_WINDOW = timedelta(days=12)
PAYMENT_EVENT_WINDOW = timedelta(days=3)
SHIPMENT_EVENT_MARGIN = timedelta(days=2)
REFUND_EVENT_MARGIN = timedelta(days=10)
START_MARGIN = timedelta(days=2)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def money(value: Any) -> Decimal | None:
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class Timeline:
    """The authoritative dates of the order under investigation."""

    purchase: datetime | None
    approved: datetime | None
    carrier: datetime | None
    delivered: datetime | None
    estimated: datetime | None

    @classmethod
    def from_order(cls, order: dict[str, Any]) -> Timeline:
        return cls(
            purchase=parse_ts(order.get("order_purchase_timestamp")),
            approved=parse_ts(order.get("order_approved_at")),
            carrier=parse_ts(order.get("order_delivered_carrier_date")),
            delivered=parse_ts(order.get("order_delivered_customer_date")),
            estimated=parse_ts(order.get("order_estimated_delivery_date")),
        )

    @property
    def start(self) -> datetime | None:
        return self.purchase or self.approved

    @property
    def end(self) -> datetime | None:
        known = [ts for ts in (self.estimated, self.delivered, self.carrier) if ts is not None]
        return max(known) if known else self.start

    @property
    def payment_anchor(self) -> datetime | None:
        return self.approved or self.purchase

    def is_late(self) -> bool:
        return (
            self.delivered is not None
            and self.estimated is not None
            and self.delivered > self.estimated
        )

    def seller_missed_handoff(self, limits: list[datetime]) -> bool:
        """True when the order left for the carrier after the agreed shipping limit."""
        return bool(limits) and self.carrier is not None and self.carrier > min(limits)

    def _between(self, moment: datetime, end_margin: timedelta) -> bool:
        start, end = self.start, self.end
        if start is None or end is None:
            return True
        return start - START_MARGIN <= moment <= end + end_margin

    def in_shipment_window(self, moment: datetime) -> bool:
        return self._between(moment, SHIPMENT_EVENT_MARGIN)

    def in_refund_window(self, moment: datetime) -> bool:
        return self._between(moment, REFUND_EVENT_MARGIN)

    def in_payment_window(self, moment: datetime) -> bool:
        anchor = self.payment_anchor
        if anchor is None:
            return True
        return abs(moment - anchor) <= PAYMENT_EVENT_WINDOW

    def in_item_window(self, moment: datetime) -> bool:
        start = self.start
        if start is None:
            return True
        return start - START_MARGIN <= moment <= start + ITEM_LIMIT_WINDOW


def scope_rows(
    rows: list[dict[str, Any]], field: str, keep: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows on a timestamp field into (in scope, out of scope)."""
    inside: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    for row in rows:
        moment = parse_ts(row.get(field))
        (inside if moment is not None and keep(moment) else outside).append(row)
    return inside, outside


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop byte-identical rows.

    Some cases return the injected variant as an exact copy of the real row, which
    would otherwise double every total.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = repr(sorted(row.items(), key=lambda pair: pair[0]))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def scope_payments(
    payments: list[dict[str, Any]], amounts: set[Decimal]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Payment rows carry no timestamp, so match them against in-scope capture amounts.

    Distinct instruments that happen to share an amount (a split across a card and a
    voucher) are both kept, because the rows themselves differ.
    """
    inside: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    for row in dedupe(payments):
        value = money(row.get("payment_value"))
        (inside if value is not None and value in amounts else outside).append(row)
    return inside, outside
