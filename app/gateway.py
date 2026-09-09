"""The core: run a tool, enforcing the human-in-the-loop gate."""
import logging

from app import approvals, registry
from app.db import Session
from app.models import AuditLog

log = logging.getLogger("gateway")


class UnknownTool(Exception):
    pass


class ToolError(Exception):
    """The tool ran and failed (bad args, DB refusal, timeout, ...)."""


async def run_tool(tool: str, args: dict) -> dict:
    entry = registry.get(tool)
    if entry is None:
        raise UnknownTool(tool)
    args = args or {}

    if entry.risk == "low":
        return await _invoke(entry, tool, args, "auto")

    approval = await approvals.create_pending(tool, args)
    log.info("awaiting approval id=%s tool=%s", approval.id, tool)
    decision = await approvals.wait_for_decision(approval.id)

    if decision == "approved":
        return await _invoke(entry, tool, args, "approved")

    await audit(tool, args, entry.risk, decision, None)
    return {"status": decision, "approval_id": str(approval.id)}


async def _invoke(entry, tool: str, args: dict, decision: str) -> dict:
    """Run the tool, auditing either way.

    A refused call is exactly what an audit log is for, so failures get a row
    with decision="error" rather than vanishing into a 500.
    """
    try:
        result = await entry.fn(**args)
    except Exception as exc:
        log.warning("tool %s failed: %s", tool, exc)
        await audit(
            tool, args, entry.risk, "error",
            {"error": str(exc)[:2000], "type": type(exc).__name__},
        )
        raise ToolError(str(exc)) from exc

    await audit(tool, args, entry.risk, decision, result)
    return result


async def audit(tool: str, args: dict, risk: str, decision: str, result) -> None:
    async with Session() as session:
        session.add(
            AuditLog(
                tool_name=tool,
                args=args,
                risk=risk,
                decision=decision,
                result=result if isinstance(result, (dict, list)) else None,
            )
        )
        await session.commit()
