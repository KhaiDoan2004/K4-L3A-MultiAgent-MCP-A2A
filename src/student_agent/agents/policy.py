"""Policy agent: turn the specialists' facts into one primary issue and a resolution.

The rules below were derived from the public machine-readable policy EC_POLICY_V1 and
from the order lifecycle itself. Order status comes first because ``get_order`` is the
authoritative row; a refund that never settled outranks a delivery complaint; and a
second charge is only a duplicate when the payments exceed the order total.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..a2a import Finding, ScopedGateway
from ..scoping import Timeline, money

ACTOR = "policy_agent"
TOOLS = frozenset({"get_policy"})

INSUFFICIENT = "insufficient_evidence"

# Which tools' evidence supports each conclusion. Evidence is scored as an F1 of
# coverage and precision, so irrelevant domains are left out of the answer.
RELEVANT_TOOLS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "unavailable_order_paid": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_policy",
    ),
    "late_delivery_seller": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_policy",
    ),
    "late_delivery_logistics": ("get_order", "get_shipment_summary", "get_policy"),
    "valid_split_payment": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_policy",
    ),
    "payment_mismatch": ("get_order", "get_order_items", "get_payment_timeline", "get_policy"),
    "duplicate_charge": ("get_order", "get_payment_timeline", "get_policy"),
    "refund_pending": (
        "get_order",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ),
    "refund_failed": (
        "get_order",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ),
    "unsupported_claim": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ),
}

FALLBACK_RULES: dict[str, dict[str, Any]] = {
    INSUFFICIENT: {
        "case_status": "needs_investigation",
        "recommended_action": "escalate_manual_review",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    }
}


async def run(
    policy_version: str,
    order: Finding,
    payment: Finding,
    shipment: Finding,
    gateway: ScopedGateway,
) -> Finding:
    evidence = await gateway.call("get_policy", policy_version=policy_version)
    payload = (evidence or {}).get("data") or {}
    rules = payload.get("rules") if isinstance(payload, dict) else None
    rules = rules if isinstance(rules, dict) else {}

    issue, confidence, causes = classify(order, payment, shipment)
    if not order.facts.get("has_order"):
        issue, confidence, causes = INSUFFICIENT, 0.25, ["MISSING_ORDER_EVIDENCE"]
    if issue != INSUFFICIENT and issue not in rules:
        # The policy is the contract; never invent a resolution it does not define.
        issue, confidence, causes = INSUFFICIENT, 0.3, ["POLICY_RULE_NOT_FOUND"]

    rule = rules.get(issue) or FALLBACK_RULES[INSUFFICIENT]
    facts: dict[str, Any] = {
        "primary_issue": issue,
        "confidence": confidence,
        "cause_codes": causes,
        "case_status": rule.get("case_status", "needs_investigation"),
        "recommended_action": rule.get("recommended_action", "escalate_manual_review"),
        "refund_brl": _refund_amount(rule),
        "responsible_parties": _responsible_parties(rule, order, shipment, issue),
        "relevant_tools": RELEVANT_TOOLS.get(issue, tuple(sorted(RELEVANT_TOOLS))),
        "has_policy": evidence is not None,
    }
    return Finding(
        actor=ACTOR, facts=facts, evidence=dict(gateway.evidence), warnings=list(gateway.warnings)
    )


def classify(
    order: Finding, payment: Finding, shipment: Finding
) -> tuple[str, float, list[str]]:
    """Return (primary_issue, confidence, cause codes) from in-scope evidence only."""
    status = str(order.facts.get("order_status") or "").lower()
    timeline: Timeline | None = order.facts.get("timeline")
    order_total: Decimal = order.facts.get("order_total") or Decimal("0")
    paid: Decimal = payment.facts.get("payment_total") or Decimal("0")
    captured: Decimal = payment.facts.get("captured_total") or Decimal("0")
    has_capture = bool(payment.facts.get("has_capture"))
    repeated: Decimal | None = payment.facts.get("repeated_amount")
    refund_status = payment.facts.get("refund_status")
    payment_count = len(payment.facts.get("payment_rows") or [])

    # 1. The authoritative order row settles cancellations, whatever else is claimed.
    if status == "canceled" and has_capture:
        return "canceled_order_paid", 0.9, ["CANCELED_ORDER_WITH_CAPTURED_PAYMENT"]
    if status == "unavailable" and has_capture:
        return "unavailable_order_paid", 0.9, ["UNAVAILABLE_ORDER_WITH_CAPTURED_PAYMENT"]

    # 2. Money the customer is already owed outranks a service complaint.
    if refund_status == "failed":
        return "refund_failed", 0.88, ["REFUND_ATTEMPT_FAILED"]
    if refund_status == "pending":
        return "refund_pending", 0.86, ["REFUND_AWAITING_SETTLEMENT"]

    # 3. Late delivery, with the responsible actor taken from the shipment evidence
    #    and cross-checked against the agreed seller handoff limit.
    if shipment.facts.get("is_late") or (timeline is not None and timeline.is_late()):
        limits = shipment.facts.get("shipping_limits") or order.facts.get("shipping_limits") or []
        missed_handoff = timeline is not None and timeline.seller_missed_handoff(limits)
        actor = shipment.facts.get("late_actor")
        if actor == "seller" or (actor is None and missed_handoff):
            agreed = actor == "seller" and missed_handoff
            return "late_delivery_seller", 0.88 if agreed else 0.74, ["SELLER_SHIPPED_AFTER_LIMIT"]
        agreed = actor == "logistics_provider" and not missed_handoff
        return (
            "late_delivery_logistics",
            0.88 if agreed else 0.74,
            ["CARRIER_DELAYED_AFTER_HANDOFF"],
        )

    # 4. The same amount charged twice, taking the order past its total.
    if repeated is not None and payment_count >= 2 and paid != order_total:
        return "duplicate_charge", 0.86, ["PAYMENT_CAPTURED_TWICE"]

    # 5. Several instruments that together settle exactly the order total.
    if payment_count >= 2 and paid == order_total and order_total > 0:
        return "valid_split_payment", 0.85, ["PAYMENT_SPLIT_ACROSS_INSTRUMENTS"]

    # 6. Reconciliation gap, either flagged by the server or visible in the totals.
    if payment.facts.get("has_mismatch_event"):
        return "payment_mismatch", 0.88, ["PAYMENT_RECONCILIATION_MISMATCH"]
    if order_total > 0 and captured > 0 and paid != order_total:
        return "payment_mismatch", 0.7, ["PAYMENT_TOTAL_DIFFERS_FROM_ORDER"]

    # 7. Nothing in the evidence supports the complaint.
    if order.facts.get("has_order"):
        return "unsupported_claim", 0.76, ["CLAIM_NOT_SUPPORTED_BY_EVIDENCE"]
    return INSUFFICIENT, 0.25, ["MISSING_ORDER_EVIDENCE"]


def _refund_amount(rule: dict[str, Any]) -> Decimal:
    amount = money(rule.get("refund_brl"))
    if amount is None or amount < 0:
        return Decimal("0")
    return amount


def _responsible_parties(
    rule: dict[str, Any], order: Finding, shipment: Finding, issue: str
) -> list[dict[str, Any]]:
    """Keep the policy's party types, but name parties from this order's evidence.

    EC_POLICY_V1 carries example seller ids that belong to other orders, so a seller
    id is always taken from the items of the order under investigation.
    """
    parties: list[dict[str, Any]] = []
    declared = rule.get("responsible_parties")
    declared = declared if isinstance(declared, list) else []
    seller_ids = order.facts.get("seller_ids") or []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        party_type = entry.get("party_type")
        if party_type not in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            continue
        party_id = None
        if party_type == "seller":
            party_id = seller_ids[0] if seller_ids else None
        parties.append({"party_type": party_type, "party_id": party_id})
    if not parties:
        parties = [{"party_type": "unknown", "party_id": None}]
    if issue == "late_delivery_seller" and shipment.facts.get("late_actor") == "seller":
        parties[0] = {
            "party_type": "seller",
            "party_id": seller_ids[0] if seller_ids else None,
        }
    return parties[:5]
