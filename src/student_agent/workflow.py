"""L3A multi-agent workflow: coordinator -> specialists (MCP) -> policy -> verifier.

Agents are in-process coroutines correlated by ``case_id``. Each specialist may only call the
tools listed in ``AGENT_TOOLS``. Business decisions live in ``rules.decide``; this module owns
evidence collection, trace lifecycle and verification.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2

from . import OUTPUT_SCHEMA_VERSION
from .contracts import ContractError, Contracts
from .mcp_gateway import EvidenceGateway
from .rules import ENTITY_KEYS, Decision, decide
from .trace import TraceWriter

COORDINATOR = "coordinator"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "order-agent": ("get_order", "get_order_items", "get_sellers", "get_product_context"),
    "payment-agent": ("get_order_payments", "get_payment_timeline"),
    "refund-agent": ("get_refund_timeline",),
    "shipment-agent": ("get_shipment_summary",),
    POLICY_AGENT: ("get_policy",),
}
EXPECTED_DOMAIN = {
    "get_order": "order",
    "get_order_items": "item",
    "get_sellers": "seller",
    "get_product_context": "product",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_policy": "policy",
}
MAX_ATTEMPTS = 2
CALL_TIMEOUT_S = 60.0
TRANSIENT_ERRORS = (TimeoutError, OSError, httpx2.TransportError)

Evidence = dict[str, dict[str, Any]]  # tool_name -> {"ref", "domain", "data"}


async def _call(
    gateway: EvidenceGateway, tool: str, case: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """Return (envelope, status). status: OK | NOT_FOUND | INVALID | TIMEOUT | ERROR."""
    if tool == "get_policy":
        arguments = {"policy_version": case["policy_version"]}
    else:
        arguments = {"order_id": case["customer_request"]["claimed_order_id"]}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            envelope = await asyncio.wait_for(
                gateway.call(tool, case_id=case["case_id"], **arguments), CALL_TIMEOUT_S
            )
        except TRANSIENT_ERRORS:
            if attempt == MAX_ATTEMPTS:
                return None, "TIMEOUT"
            await asyncio.sleep(0.5 * attempt)
            continue
        except RuntimeError:  # tool-level error from the server: no data for this order
            return None, "NOT_FOUND"
        except (ValueError, ContractError):  # malformed evidence envelope
            return None, "INVALID"
        except Exception:  # unknown client/protocol error: missing evidence, never a crash
            return None, "ERROR"
        if envelope.get("domain") != EXPECTED_DOMAIN[tool]:
            return None, "INVALID"
        return envelope, "OK"
    return None, "TIMEOUT"


async def _specialist(
    agent: str,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    store: Evidence,
) -> None:
    case_id = case["case_id"]
    statuses: dict[str, str] = {}
    for tool in AGENT_TOOLS[agent]:
        envelope, statuses[tool] = await _call(gateway, tool, case)
        if envelope is None:
            continue
        ref = envelope["evidence_ref"]
        store[tool] = {"ref": ref, "domain": envelope["domain"], "data": envelope["data"]}
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=agent,
            tool_name=tool,
            evidence_refs=[ref],
        )
    ok = sum(status == "OK" for status in statuses.values())
    code = (
        "EVIDENCE_READY"
        if ok == len(statuses)
        else "EVIDENCE_PARTIAL" if ok else "EVIDENCE_MISSING"
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=agent,
        target=COORDINATOR,
        decision_code=code,
        attributes={"tools_ok": ok, "tools_total": len(statuses), **statuses},
    )


def _refs(store: Evidence, tools: list[str]) -> list[str]:
    return list(dict.fromkeys(store[t]["ref"] for t in tools if t in store))


def _build_output(case: dict[str, Any], decision: Decision, store: Evidence) -> dict[str, Any]:
    lines = [dict(line) for line in decision.refund_lines]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": {key: list(decision.entities.get(key, [])) for key in ENTITY_KEYS},
        "claim_assessments": [
            {
                "claim_id": claim.claim_id,
                "verdict": claim.verdict,
                "confidence": claim.confidence,
                "evidence_refs": _refs(store, claim.cite_tools),
            }
            for claim in decision.claims
        ],
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": code, "rank": rank}
                for rank, code in enumerate(decision.cause_codes[:5], 1)
            ],
            "responsible_parties": [dict(party) for party in decision.responsible_parties],
        },
        "evidence_refs": _refs(store, decision.cite_tools),
        "data_conflicts": [dict(conflict) for conflict in decision.data_conflicts],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(sum(line["amount_brl"] for line in lines), 2),
            "refund_lines": lines,
        },
        "resolution_actions": list(decision.actions),
    }


def _clamp(value: Any) -> float:
    return min(1.0, max(0.0, float(value)))


def verify(
    output: dict[str, Any], case: dict[str, Any], store: Evidence, contracts: Contracts
) -> tuple[dict[str, Any], str, dict[str, int | bool]]:
    """Enforce invariants. Returns (output, PASS|FIXED|DOWNGRADED, check counts)."""
    try:
        return _verify(output, case, store, contracts)
    except (KeyError, TypeError, ValueError, AttributeError):  # malformed decision shape
        output = _safe_output(case, store)
        contracts.validate_output(output, f"outputs/{case['case_id']}.json")
        return output, "DOWNGRADED", {"checks_run": 9, "fixes_applied": 0, "downgraded": True}


def _verify(
    output: dict[str, Any], case: dict[str, Any], store: Evidence, contracts: Contracts
) -> tuple[dict[str, Any], str, dict[str, int | bool]]:
    fixes = 0
    known = {entry["ref"] for entry in store.values()}

    def keep_known(refs: list[str]) -> list[str]:
        nonlocal fixes
        kept = [ref for ref in dict.fromkeys(refs) if ref in known]
        fixes += len(kept) != len(refs)
        return kept

    output["case_id"] = case["case_id"]
    output["evidence_refs"] = keep_known(output["evidence_refs"])
    if not output["evidence_refs"] and "get_order" in store:
        output["evidence_refs"] = [store["get_order"]["ref"]]
        fixes += 1
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = keep_known(claim["evidence_refs"])
        claim["confidence"] = _clamp(claim["confidence"])
    assessment = output["assessment"]
    assessment["confidence"] = _clamp(assessment["confidence"])

    entities = output["affected_entities"]
    for key in ENTITY_KEYS:
        cleaned = list(dict.fromkeys(i for i in entities[key] if isinstance(i, str) and i))[:20]
        fixes += cleaned != entities[key]
        entities[key] = cleaned
    sellers = entities["seller_ids"]
    for party in output["root_cause_analysis"]["responsible_parties"]:
        seller_party = party.get("party_type") == "seller"
        if seller_party and party.get("party_id") not in sellers and len(sellers) == 1:
            party["party_id"] = sellers[0]
            fixes += 1

    actions = list(dict.fromkeys(output["resolution_actions"]))[:8]
    fixes += actions != output["resolution_actions"]
    financial = output["financial_resolution"]
    if assessment["case_status"] == "no_action":
        if financial["refund_lines"] or any("refund" in a for a in actions):
            fixes += 1
        financial["refund_lines"] = []
        actions = [a for a in actions if "refund" not in a]
    total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    fixes += financial["recommended_refund_brl"] != total
    financial["recommended_refund_brl"] = total
    output["resolution_actions"] = actions

    downgraded = False
    if assessment["case_status"] == "action_required" and not actions:
        assessment["case_status"] = "needs_investigation"
        downgraded = True

    try:
        contracts.validate_output(output, f"outputs/{case['case_id']}.json")
    except ContractError:
        output = _safe_output(case, store)
        contracts.validate_output(output, f"outputs/{case['case_id']}.json")
        downgraded = True

    code = "DOWNGRADED" if downgraded else "FIXED" if fixes else "PASS"
    return output, code, {"checks_run": 9, "fixes_applied": fixes, "downgraded": downgraded}


def _safe_output(case: dict[str, Any], store: Evidence) -> dict[str, Any]:
    order_id = case["customer_request"]["claimed_order_id"]
    decision = Decision(
        primary_issue="insufficient_evidence",
        case_status="needs_investigation",
        confidence=0.2,
        actions=["request_more_evidence"],
        responsible_parties=[{"party_type": "unknown", "party_id": None}],
        cause_codes=["INSUFFICIENT_EVIDENCE"],
        entities={key: [order_id] if key == "order_ids" else [] for key in ENTITY_KEYS},
        cite_tools=["get_order"],
    )
    output = _build_output(case, decision, store)
    output.pop("claim_assessments")
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    store: Evidence = {}  # per-case evidence store; never shared across cases

    for agent in AGENT_TOOLS:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=agent,
            attributes={"tools": ",".join(AGENT_TOOLS[agent])},
        )
    await asyncio.gather(
        *(_specialist(agent, case, gateway, trace, store) for agent in AGENT_TOOLS)
    )

    # Pure decision over data only; rules never see refs.
    try:
        decision = decide(case, {tool: entry["data"] for tool, entry in store.items()})
        output = _build_output(case, decision, store)
        rules_ok = True
    except Exception:  # a rules bug must not abort the 100-case run
        output, rules_ok = _safe_output(case, store), False
    assessment = output["assessment"]
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor=POLICY_AGENT,
        decision_code=assessment["primary_issue"],
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={
            "case_status": assessment["case_status"],
            "confidence": assessment["confidence"],
            "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
            "rules_ok": rules_ok,
        },
    )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=COORDINATOR,
        target=VERIFIER,
        decision_code="VERIFY_OUTPUT",
    )
    output, code, checks = verify(output, case, store, trace.contracts)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code=code,
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={**checks, "primary_issue": output["assessment"]["primary_issue"]},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=VERIFIER,
        target=COORDINATOR,
        decision_code=f"OUTPUT_{code}",
    )
    return output

