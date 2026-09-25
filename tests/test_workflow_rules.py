"""Unit tests for the decision rules and the verifier. No network, no .env needed."""

from __future__ import annotations

from decimal import Decimal

from student_agent.a2a import Finding
from student_agent.agents.policy import classify
from student_agent.agents.verifier import verify
from student_agent.scoping import Timeline, dedupe, parse_ts, scope_payments

PURCHASE = "2018-06-25T09:00:00-03:00"
APPROVED = "2018-06-25T10:00:00-03:00"


def order_finding(**overrides) -> Finding:
    facts = {
        "order_id": "order-1",
        "order_status": "delivered",
        "has_order": True,
        "item_ids": ["item-1"],
        "seller_ids": ["seller-1"],
        "order_total": Decimal("89.00"),
        "shipping_limits": [],
        "timeline": Timeline.from_order(
            {
                "order_purchase_timestamp": PURCHASE,
                "order_approved_at": APPROVED,
                "order_delivered_carrier_date": "2018-06-27T09:00:00-03:00",
                "order_delivered_customer_date": "2018-07-04T09:00:00-03:00",
                "order_estimated_delivery_date": "2018-07-05T09:00:00-03:00",
            }
        ),
    }
    facts.update(overrides)
    return Finding(actor="order_item_agent", facts=facts)


def payment_finding(**overrides) -> Finding:
    facts = {
        "payment_rows": [{"payment_value": "89.00"}],
        "payment_total": Decimal("89.00"),
        "captured_total": Decimal("89.00"),
        "has_capture": True,
        "repeated_amount": None,
        "has_mismatch_event": False,
        "refund_status": None,
    }
    facts.update(overrides)
    return Finding(actor="payment_agent", facts=facts)


def shipment_finding(**overrides) -> Finding:
    facts = {"is_late": False, "late_actor": None, "shipping_limits": []}
    facts.update(overrides)
    return Finding(actor="shipment_agent", facts=facts)


def issue_of(order: Finding, paid: Finding, shipped: Finding) -> str:
    return classify(order, paid, shipped)[0]


def test_canceled_order_with_payment_beats_a_stale_delivery_event() -> None:
    order = order_finding(order_status="canceled")
    shipped = shipment_finding(is_late=True, late_actor="seller")
    assert issue_of(order, payment_finding(), shipped) == "canceled_order_paid"


def test_unavailable_order_with_payment() -> None:
    order = order_finding(order_status="unavailable")
    assert issue_of(order, payment_finding(), shipment_finding()) == "unavailable_order_paid"


def test_failed_refund_outranks_a_delivery_complaint() -> None:
    paid = payment_finding(refund_status="failed")
    shipped = shipment_finding(is_late=True, late_actor="logistics_provider")
    assert issue_of(order_finding(), paid, shipped) == "refund_failed"


def test_pending_refund() -> None:
    paid = payment_finding(refund_status="pending")
    assert issue_of(order_finding(), paid, shipment_finding()) == "refund_pending"


def test_late_delivery_is_attributed_to_the_seller_who_missed_the_handoff() -> None:
    # Carrier pickup (2018-06-27) is after the agreed limit, so the seller shipped late.
    shipped = shipment_finding(
        is_late=True,
        late_actor="seller",
        shipping_limits=[parse_ts("2018-06-26T09:00:00-03:00")],
    )
    issue, confidence, _ = classify(order_finding(), payment_finding(), shipped)
    assert issue == "late_delivery_seller"
    assert confidence > 0.8  # both signals agree


def test_late_delivery_falls_to_logistics_when_the_seller_met_the_limit() -> None:
    shipped = shipment_finding(is_late=True, late_actor="logistics_provider")
    assert issue_of(order_finding(), payment_finding(), shipped) == "late_delivery_logistics"


def test_two_equal_charges_above_the_order_total_are_a_duplicate() -> None:
    paid = payment_finding(
        payment_rows=[{"payment_value": "64.00"}, {"payment_value": "64.00"}],
        payment_total=Decimal("128.00"),
        captured_total=Decimal("128.00"),
        repeated_amount=Decimal("64.00"),
    )
    assert issue_of(order_finding(), paid, shipment_finding()) == "duplicate_charge"


def test_two_equal_charges_that_settle_the_order_total_are_a_valid_split() -> None:
    paid = payment_finding(
        payment_rows=[{"payment_value": "44.50"}, {"payment_value": "44.50"}],
        payment_total=Decimal("89.00"),
        captured_total=Decimal("89.00"),
        repeated_amount=Decimal("44.50"),
    )
    assert issue_of(order_finding(), paid, shipment_finding()) == "valid_split_payment"


def test_reconciliation_event_reports_a_payment_mismatch() -> None:
    paid = payment_finding(
        payment_rows=[{"payment_value": "35.00"}],
        payment_total=Decimal("35.00"),
        captured_total=Decimal("35.00"),
        has_mismatch_event=True,
    )
    assert issue_of(order_finding(), paid, shipment_finding()) == "payment_mismatch"


def test_a_healthy_order_does_not_support_the_claim() -> None:
    assert issue_of(order_finding(), payment_finding(), shipment_finding()) == "unsupported_claim"


def test_a_missing_order_row_yields_insufficient_evidence() -> None:
    order = order_finding(has_order=False, order_status=None, order_total=Decimal("0"))
    paid = payment_finding(has_capture=False, payment_total=Decimal("0"),
                           captured_total=Decimal("0"), payment_rows=[])
    assert issue_of(order, paid, shipment_finding()) == "insufficient_evidence"


def test_identical_rows_are_collapsed_before_totals_are_computed() -> None:
    row = {"payment_sequential": "1", "payment_value": "89.00"}
    assert dedupe([row, dict(row)]) == [row]


def test_distinct_instruments_sharing_an_amount_are_both_kept() -> None:
    rows = [
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "44.50"},
        {"payment_sequential": "2", "payment_type": "voucher", "payment_value": "44.50"},
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "52.00"},
    ]
    inside, outside = scope_payments(rows, {Decimal("44.50")})
    assert len(inside) == 2
    assert len(outside) == 1


REF_A = "ev_" + "a" * 30
REF_B = "ev_" + "b" * 30


def draft(**overrides) -> dict:
    value = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": "L3A_CASE_001",
        "assessment": {
            "primary_issue": "canceled_order_paid",
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["order-1"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CANCELED_ORDER_PAID", "rank": 1}],
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        },
        "evidence_refs": [REF_A],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 79.0,
            "refund_lines": [
                {"reason_code": "issue_refund", "amount_brl": 79.0, "entity_id": "order-1"}
            ],
        },
        "resolution_actions": ["issue_refund"],
    }
    value.update(overrides)
    return value


def test_verifier_passes_a_consistent_draft() -> None:
    output, decision = verify(draft(), {REF_A})
    assert decision == "pass"
    assert output["evidence_refs"] == [REF_A]


def test_verifier_drops_an_evidence_ref_from_another_case() -> None:
    output, decision = verify(draft(evidence_refs=[REF_A, REF_B]), {REF_A})
    assert output["evidence_refs"] == [REF_A]
    assert decision == "fail_evidence_ref_scope"


def test_verifier_realigns_a_refund_total_with_its_lines() -> None:
    financial = {
        "currency": "BRL",
        "recommended_refund_brl": 100.0,
        "refund_lines": [
            {"reason_code": "issue_refund", "amount_brl": 79.0, "entity_id": "order-1"}
        ],
    }
    output, decision = verify(draft(financial_resolution=financial), {REF_A})
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert decision == "fail_refund_lines_total"


def test_verifier_clamps_confidence_and_unknown_enums() -> None:
    assessment = {"primary_issue": "not_an_issue", "case_status": "weird", "confidence": 5}
    output, _ = verify(draft(assessment=assessment), {REF_A})
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["confidence"] == 1.0
