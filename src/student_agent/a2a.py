"""Agent-to-agent plumbing: message envelope, per-agent tool scoping, resilient calls.

The competition only observes results, evidence refs and trace events, so the agents
here are plain async functions in one process. This module gives them a shared
message type (``Finding``) and a gateway view that enforces tool permissions,
retries transport failures and emits ``tool_result_consumed`` automatically.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcp_gateway import EvidenceGateway, connect_gateway
from .trace import TraceWriter

# gateway.call() raises RuntimeError when the server reports a tool error and
# ValueError (ContractError) when the envelope fails schema validation. Neither is
# fixed by retrying or reconnecting; everything else is a transport problem.
DATA_ERRORS = (RuntimeError, ValueError)

MAX_ATTEMPTS = 3
# One session per case plus room for retries across a 100-case run.
MAX_REBUILDS = 600


@dataclass
class Finding:
    """One specialist's report, handed to the next actor in the workflow."""

    actor: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def evidence_refs(self) -> list[str]:
        """Server-issued refs collected by this agent, de-duplicated, order kept."""
        return list(dict.fromkeys(self.evidence.values()))


class ResilientGateway:
    """Owns its own MCP session instead of borrowing the one the CLI opened.

    This matters for reliability. ``streamable_http_client`` runs its transport in an
    anyio task group whose cancel scope wraps the caller's body, so a dropped
    connection cancels the CLI's own loop and no ``except Exception`` inside
    ``solve_case`` can contain it. By opening and closing a session here, a transport
    failure only cancels this class's scope, which it then retries. ``day09 run``
    wipes outputs and the trace on every invocation, so surviving a flaky connection
    is the difference between 100 answers and none.
    """

    def __init__(self, gateway: EvidenceGateway) -> None:
        self.origin = gateway
        self._contracts = gateway._contracts
        self._gateway: EvidenceGateway | None = None
        self._stack: AsyncExitStack | None = None
        self.rebuilds = 0

    @property
    def _root(self) -> Path:
        # Contracts points at <root>/contracts/schemas.
        return self._contracts.root.parent.parent

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        for attempt in range(MAX_ATTEMPTS):
            gateway = self._gateway or await self._open()
            try:
                return await gateway.call(tool_name, case_id=case_id, **arguments)
            except DATA_ERRORS:
                raise
            except BaseException:
                # Either the call failed or our own task group was torn down under us.
                await self.close()
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        """Drop the session; the next call opens a fresh one."""
        stack, self._stack, self._gateway = self._stack, None, None
        if stack is not None:
            with suppress(BaseException):
                await stack.aclose()

    async def _open(self) -> EvidenceGateway:
        if self.rebuilds >= MAX_REBUILDS:
            raise RuntimeError("MCP reconnect budget exhausted")
        self.rebuilds += 1
        # Imported lazily so this module stays importable without a populated .env.
        from .config import Settings

        settings = Settings.load(self._root)
        stack = AsyncExitStack()
        gateway = await stack.enter_async_context(
            connect_gateway(settings.mcp_endpoint, settings.team_api_key, self._contracts)
        )
        self._stack, self._gateway = stack, gateway
        return gateway


class ScopedGateway:
    """The only gateway an agent sees: allow-listed tools, retries, automatic trace."""

    def __init__(
        self,
        actor: str,
        case_id: str,
        gateway: ResilientGateway,
        trace: TraceWriter,
        allowed_tools: frozenset[str],
    ) -> None:
        self.actor = actor
        self.case_id = case_id
        self._gateway = gateway
        self._trace = trace
        self._allowed = allowed_tools
        self.evidence: dict[str, str] = {}
        self.warnings: list[str] = []

    async def call(self, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        """Return a validated evidence envelope, or None when the tool has no answer."""
        if tool_name not in self._allowed:
            raise PermissionError(f"{self.actor} may not call {tool_name}")

        evidence: dict[str, Any] | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                evidence = await self._gateway.call(tool_name, case_id=self.case_id, **arguments)
                break
            except DATA_ERRORS as exc:
                # e.g. get_refund_timeline on an order that never had a refund.
                self.warnings.append(f"{tool_name}: {type(exc).__name__}")
                return None
            except Exception as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    self.warnings.append(f"{tool_name}: {type(exc).__name__}")
                    return None
                await asyncio.sleep(0.5 * 2**attempt)

        if evidence is None:
            return None

        self.evidence[tool_name] = evidence["evidence_ref"]
        for warning in evidence.get("warnings") or []:
            self.warnings.append(f"{tool_name}: {warning}")
        self._trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=self.actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence
