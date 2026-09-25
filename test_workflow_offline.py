from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import AGENT_TOOLS, verify

ROOT = Path(__file__).resolve().parents[1]
DUMP = json.loads((ROOT / "tests" / "fixtures" / "L3A_CASE_001_dump.json").read_text())


class FakeGateway:
    """Serves recorded MCP envelopes; a recorded error becomes a tool-level RuntimeError."""

    def __init__(self, dump: dict[str, Any]) -> None:
        self.dump = dump
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id))
        entry = self.dump[tool_name]
        if "error" in entry:
            raise RuntimeError(entry["error"])
        return entry


def run_case(tmp_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], FakeGateway]:
    from student_agent.workflow import solve_case

    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = DUMP["input"]
    gateway = FakeGateway(DUMP)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    return output, events, gateway


def test_offline_case_validates_and_links_evidence(tmp_path: Path) -> None:
    output, events, gateway = run_case(tmp_path)
    assert output["case_id"] == "L3A_CASE_001"
    assert {case_id for _, case_id in gateway.calls} == {"L3A_CASE_001"}

    types = [event["event_type"] for event in events]
    for required in ("case_received", "task_assigned", "handoff", "verification_completed"):
        assert required in types
    assert types[0] == "case_received" and types[-1] == "case_finalized"
    assert "policy_decided" in types
    assert len({event["actor"] for event in events}) >= 4

    consumed = [e for e in events if e["event_type"] == "tool_result_consumed"]
    for event in consumed:
        assert event["tool_name"] in AGENT_TOOLS[event["actor"]]
    # refund timeline errored in the dump: no consumption, no crash
    assert "get_refund_timeline" not in {e["tool_name"] for e in consumed}
    consumed_refs = {ref for e in consumed for ref in e["evidence_refs"]}
    output_refs = set(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        output_refs |= set(claim["evidence_refs"])
    assert output_refs and output_refs <= consumed_refs


def test_verifier_repairs_inconsistent_output(tmp_path: Path) -> None:
    output, _, _ = run_case(tmp_path)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    store = {"get_order": {"ref": DUMP["get_order"]["evidence_ref"], "domain": "order", "data": {}}}
    output["evidence_refs"] = ["ev_forged_reference_000000000", store["get_order"]["ref"]]
    output["claim_assessments"] = []
    output["assessment"].update(case_status="no_action", confidence=1.7)
    output["resolution_actions"] = ["issue_refund", "issue_refund"]
    fixed, code, _ = verify(output, DUMP["input"], store, contracts)
    assert code in {"FIXED", "DOWNGRADED"}
    assert fixed["evidence_refs"] == [store["get_order"]["ref"]]
    assert fixed["assessment"]["confidence"] == 1.0
    assert fixed["financial_resolution"]["recommended_refund_brl"] == 0
    assert fixed["resolution_actions"] == []
