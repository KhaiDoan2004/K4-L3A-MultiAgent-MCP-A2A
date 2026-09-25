"""Verifier agent: last gate before an answer leaves the workflow.

``day09 run`` validates every output against the public schema and aborts the whole
run on the first failure, so this module both checks business invariants and clamps
the draft to the schema's limits.
"""

from __future__ import annotations

import re
from typing import Any

CAUSE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
PARTY_TYPES = {
    "seller",
    "platform",
    "logistics_provider",
    "payment_provider",
    "customer",
    "unknown",
}
CASE_STATUSES = {"action_required", "no_action", "needs_investigation"}
VERDICTS = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}
PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}
MONEY_TOLERANCE = 0.005

ACTOR = "verifier_agent"


def verify(draft: dict[str, Any], allowed_refs: set[str]) -> tuple[dict[str, Any], str]:
    """Return (safe output, decision code). Never raises; never invents evidence."""
    problems: list[str] = []
    output = _sanitize(draft, allowed_refs, problems)

    assessment = output["assessment"]
    financial = output["financial_resolution"]
    lines_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if abs(lines_total - financial["recommended_refund_brl"]) > MONEY_TOLERANCE:
        problems.append("refund_lines_total")
        financial["recommended_refund_brl"] = lines_total
    if assessment["case_status"] == "no_action" and financial["recommended_refund_brl"] > 0:
        problems.append("no_action_with_refund")
    if assessment["primary_issue"] == "insufficient_evidence" and financial[
        "recommended_refund_brl"
    ] > 0:
        problems.append("insufficient_evidence_with_refund")
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
    if not output["evidence_refs"]:
        problems.append("no_evidence")

    if problems:
        return output, f"fail_{problems[0]}"[:80]
    return output, "pass"


def _sanitize(draft: dict[str, Any], allowed_refs: set[str], problems: list[str]) -> dict[str, Any]:
    assessment = draft.get("assessment") or {}
    issue = assessment.get("primary_issue")
    if issue not in PRIMARY_ISSUES:
        problems.append("primary_issue")
        issue = "insufficient_evidence"
    status = assessment.get("case_status")
    if status not in CASE_STATUSES:
        problems.append("case_status")
        status = "needs_investigation"

    refs = _refs(draft.get("evidence_refs"), allowed_refs, problems)
    entities = draft.get("affected_entities") or {}
    financial = draft.get("financial_resolution") or {}
    root_cause = draft.get("root_cause_analysis") or {}

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": draft.get("case_id"),
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": _confidence(assessment.get("confidence")),
        },
        "affected_entities": {
            name: _id_set(entities.get(name)) for name in
            ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
        },
        "root_cause_analysis": {
            "ranked_causes": _ranked_causes(root_cause.get("ranked_causes")),
            "responsible_parties": _parties(root_cause.get("responsible_parties")),
        },
        "evidence_refs": refs,
        "data_conflicts": _conflicts(draft.get("data_conflicts")),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _amount(financial.get("recommended_refund_brl")),
            "refund_lines": _refund_lines(financial.get("refund_lines")),
        },
        "resolution_actions": _actions(draft.get("resolution_actions")),
    }
    claims = _claims(draft.get("claim_assessments"), allowed_refs)
    if claims:
        output["claim_assessments"] = claims
    return output


def _confidence(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.3
    return round(min(1.0, max(0.0, float(value))), 4)


def _amount(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, number), 2)


def _refs(value: Any, allowed_refs: set[str], problems: list[str]) -> list[str]:
    refs: list[str] = []
    for ref in value if isinstance(value, list) else []:
        if not isinstance(ref, str) or not EVIDENCE_REF.fullmatch(ref):
            problems.append("evidence_ref_format")
            continue
        if ref not in allowed_refs:
            # Submitting a ref this case did not obtain is a hard scoring gate.
            problems.append("evidence_ref_scope")
            continue
        if ref not in refs:
            refs.append(ref)
    return refs[:30]


def _id_set(value: Any) -> list[str]:
    ids: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = str(item) if item is not None else ""
        if text and len(text) <= 128 and text not in ids:
            ids.append(text)
    return ids[:20]


def _ranked_causes(value: Any) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    for entry in value if isinstance(value, list) else []:
        code = entry.get("cause_code") if isinstance(entry, dict) else None
        if isinstance(code, str) and CAUSE_CODE.fullmatch(code):
            causes.append({"cause_code": code, "rank": len(causes) + 1})
        if len(causes) == 5:
            break
    return causes


def _parties(value: Any) -> list[dict[str, Any]]:
    parties: list[dict[str, Any]] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or entry.get("party_type") not in PARTY_TYPES:
            continue
        party_id = entry.get("party_id")
        party_id = party_id if isinstance(party_id, str) and party_id[:128] else None
        parties.append(
            {"party_type": entry["party_type"], "party_id": party_id[:128] if party_id else None}
        )
        if len(parties) == 5:
            break
    return parties or [{"party_type": "unknown", "party_id": None}]


def _conflicts(value: Any) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        field = str(entry.get("field") or "")[:100]
        sources = [str(source)[:80] for source in entry.get("sources") or [] if source]
        sources = list(dict.fromkeys(sources))
        code = str(entry.get("resolution_code") or "")[:80]
        if not field or len(sources) < 2 or not code:
            continue
        selected = entry.get("selected_source")
        conflicts.append(
            {
                "field": field,
                "sources": sources[:5],
                "selected_source": str(selected)[:80] if selected else None,
                "resolution_code": code,
            }
        )
        if len(conflicts) == 5:
            break
    return conflicts


def _refund_lines(value: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason_code") or "")[:80]
        if not reason:
            continue
        entity = entry.get("entity_id")
        lines.append(
            {
                "reason_code": reason,
                "amount_brl": _amount(entry.get("amount_brl")),
                "entity_id": str(entity)[:128] if entity else None,
            }
        )
        if len(lines) == 10:
            break
    return lines


def _actions(value: Any) -> list[str]:
    actions: list[str] = []
    for entry in value if isinstance(value, list) else []:
        text = str(entry or "").strip()[:80]
        if text and text not in actions:
            actions.append(text)
    return actions[:8] or ["escalate_manual_review"]


def _claims(value: Any, allowed_refs: set[str]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        claim_id = str(entry.get("claim_id") or "")[:64]
        verdict = entry.get("verdict")
        if not claim_id or verdict not in VERDICTS:
            continue
        refs = [
            ref
            for ref in entry.get("evidence_refs") or []
            if isinstance(ref, str) and ref in allowed_refs and EVIDENCE_REF.fullmatch(ref)
        ]
        claims.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": _confidence(entry.get("confidence")),
                "evidence_refs": list(dict.fromkeys(refs))[:30],
            }
        )
        if len(claims) == 5:
            break
    return claims
