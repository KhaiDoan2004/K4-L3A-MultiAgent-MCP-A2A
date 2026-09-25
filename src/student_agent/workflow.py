"""Coordinator for the L3A multi-agent dispute workflow.

Flow, one pass per case, with the matching observable trace event in brackets:

    coordinator          [case_received, emitted by the CLI]
      -> order_item      [task_assigned] get_order, get_order_items
      -> payment         [task_assigned] get_payment_timeline, get_refund_timeline
      -> shipment        [task_assigned] get_shipment_summary
      -> policy          [task_assigned] get_policy, then [policy_decided]
      -> verifier        [handoff] invariants, then [verification_completed]
    coordinator          [case_finalized, emitted by the CLI]

``day09 run`` has no per-case error handling and deletes previous outputs on every
invocation, so this function must always return a schema-valid answer.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal
from typing import Any

from .a2a import Finding, ResilientGateway, ScopedGateway
from .agents import order_item, payment, policy, shipment, verifier
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"

# Claim topics that describe the same symptom as another primary issue. When the
# customer named a sibling of the real cause, the claim is partially supported.
CLAIM_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"late_delivery_seller", "late_delivery_logistics"}),
    frozenset({"refund_pending", "refund_failed"}),
    frozenset({"canceled_order_paid", "unavailable_order_paid"}),
    frozenset({"payment_mismatch", "duplicate_charge", "valid_split_payment"}),
)
FULL_REFUND_TOPIC = "requested_full_refund"
CASE_ATTEMPTS = 3

_POOL: ResilientGateway | None = None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one dispute and return a ``day09-l3a-output-v2`` document."""
    case_id = str(case.get("case_id") or "")
    pool = _pool(gateway)
    output = _unresolved_output(case_id)
    decision = "fail_not_attempted"

    # The gateway drops connections often enough that a case can lose its evidence
    # through no fault of the reasoning. Re-investigate such a case on a fresh session.
    for attempt in range(CASE_ATTEMPTS):
        try:
            output, decision = await _investigate(case, case_id, pool, trace)
        except Exception as exc:  # noqa: BLE001 - one bad case must not abort the run
            output, decision = _unresolved_output(case_id), f"fail_{type(exc).__name__}"[:80]
        finally:
            # Close our session here, so its cancel scope never spans the CLI's own
            # work between cases.
            with suppress(Exception):
                await pool.close()
        if _is_conclusive(output) or attempt == CASE_ATTEMPTS - 1:
            break
        await asyncio.sleep(2.0 * (attempt + 1))

    with suppress(Exception):
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=verifier.ACTOR,
            decision_code=decision,
            evidence_refs=output["evidence_refs"][:20] or None,
        )
    return output


def _is_conclusive(output: dict[str, Any]) -> bool:
    """True when the answer rests on evidence rather than on a failed lookup."""
    return bool(output["evidence_refs"]) and (
        output["assessment"]["primary_issue"] != "insufficient_evidence"
    )


def _pool(gateway: EvidenceGateway) -> ResilientGateway:
    """Reuse one reconnecting session across the whole run."""
    global _POOL
    if _POOL is None or _POOL.origin is not gateway:
        _POOL = ResilientGateway(gateway)
    return _POOL


async def _investigate(
    case: dict[str, Any], case_id: str, pool: ResilientGateway, trace: TraceWriter
) -> tuple[dict[str, Any], str]:
    request = case.get("customer_request") or {}
    order_id = str(request.get("claimed_order_id") or "")
    policy_version = str(case.get("policy_version") or "EC_POLICY_V1")

    def scoped(actor: str, tools: frozenset[str]) -> ScopedGateway:
        trace.emit(
            case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=actor
        )
        return ScopedGateway(actor, case_id, pool, trace, tools)

    def handoff(source: Finding, target: str) -> None:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=source.actor,
            target=target,
            evidence_refs=source.evidence_refs[:20] or None,
        )

    order = await order_item.run(order_id, scoped(order_item.ACTOR, order_item.TOOLS))
    handoff(order, policy.ACTOR)
    timeline = order.facts["timeline"]

    paid = await payment.run(order_id, timeline, scoped(payment.ACTOR, payment.TOOLS))
    handoff(paid, policy.ACTOR)

    shipped = await shipment.run(order_id, timeline, scoped(shipment.ACTOR, shipment.TOOLS))
    handoff(shipped, policy.ACTOR)

    decided = await policy.run(
        policy_version, order, paid, shipped, scoped(policy.ACTOR, policy.TOOLS)
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor=policy.ACTOR,
        decision_code=str(decided.facts["primary_issue"]),
        attributes={"cause_code": str((decided.facts["cause_codes"] or ["UNKNOWN"])[0])},
    )
    handoff(decided, verifier.ACTOR)

    findings = (order, paid, shipped, decided)
    draft = _draft(case, case_id, order_id, findings)
    allowed = {ref for finding in findings for ref in finding.evidence.values()}
    return verifier.verify(draft, allowed)


def _draft(
    case: dict[str, Any], case_id: str, order_id: str, findings: tuple[Finding, ...]
) -> dict[str, Any]:
    order, paid, shipped, decided = findings
    facts = decided.facts
    issue = str(facts["primary_issue"])
    refund: Decimal = facts["refund_brl"]

    refs_by_tool: dict[str, str] = {}
    for finding in findings:
        refs_by_tool.update(finding.evidence)
    refs = [refs_by_tool[tool] for tool in facts["relevant_tools"] if tool in refs_by_tool]
    if not refs:
        refs = list(dict.fromkeys(refs_by_tool.values()))

    conflicts: list[dict[str, Any]] = []
    for finding in (order, paid, shipped):
        conflicts.extend(finding.facts.get("conflicts") or [])

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines = [
            {
                "reason_code": str(facts["recommended_action"]),
                "amount_brl": float(refund),
                "entity_id": order_id or None,
            }
        ]

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": str(facts["case_status"]),
            "confidence": float(facts["confidence"]),
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": order.facts.get("item_ids") or [],
            "seller_ids": order.facts.get("seller_ids") or [],
            "payment_references": paid.facts.get("payment_references") or [],
            "shipment_ids": [order_id] if shipped.facts.get("has_shipment_evidence") else [],
        },
        "claim_assessments": _claims(case, issue, refund, order, refs),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": facts["responsible_parties"],
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts[:3],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [str(facts["recommended_action"])],
    }


def _claims(
    case: dict[str, Any],
    issue: str,
    refund: Decimal,
    order: Finding,
    refs: list[str],
) -> list[dict[str, Any]]:
    claims = (case.get("customer_request") or {}).get("claims") or []
    order_total: Decimal = order.facts.get("order_total") or Decimal("0")
    assessed: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id:
            continue
        topic = str(claim.get("topic") or "")
        if topic == FULL_REFUND_TOPIC:
            verdict, confidence = _refund_verdict(refund, order_total)
        else:
            verdict, confidence = _topic_verdict(topic, issue)
        assessed.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    return assessed


def _refund_verdict(refund: Decimal, order_total: Decimal) -> tuple[str, float]:
    if refund <= 0:
        return "unsupported", 0.8
    if order_total > 0 and refund >= order_total:
        return "supported", 0.82
    return "partially_supported", 0.7


def _topic_verdict(topic: str, issue: str) -> tuple[str, float]:
    if issue == "insufficient_evidence":
        return "insufficient_evidence", 0.3
    if topic == issue:
        return "supported", 0.85
    if any(topic in family and issue in family for family in CLAIM_FAMILIES):
        return "partially_supported", 0.6
    return "unsupported", 0.78


def _unresolved_output(case_id: str) -> dict[str, Any]:
    """Schema-valid answer for a case the workflow could not complete."""
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["escalate_manual_review"],
    }
