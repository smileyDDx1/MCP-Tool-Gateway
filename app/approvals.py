"""In-memory approval store.

One asyncio.Event per pending call. The waiting /call request awaits it under a
timeout; the dashboard's approve/deny sets it. Single process on purpose --
approver and waiter live in the same event loop, so no Redis is needed.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import Session
from app.models import PendingApproval

APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT", "300"))

events: dict[str, asyncio.Event] = {}
decisions: dict[str, str] = {}


async def create_pending(tool_name: str, args: dict) -> PendingApproval:
    """Write a pending row and arm its event before anyone can see the row."""
    row = PendingApproval(id=uuid.uuid4(), tool_name=tool_name, args=args, status="pending")
    events[str(row.id)] = asyncio.Event()
    async with Session() as session:
        session.add(row)
        await session.commit()
    return row


async def wait_for_decision(approval_id, timeout: int | None = None) -> str:
    """Block until approve/deny, or return "timeout"."""
    key = str(approval_id)
    event = events.get(key)
    if event is None:  # already resolved, or lost to a restart
        return decisions.get(key, "timeout")

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout or APPROVAL_TIMEOUT)
        decision = decisions.get(key, "denied")
    except asyncio.TimeoutError:
        decision = "timeout"
        # Nobody is coming: close the row out so it leaves the dashboard.
        await _record(key, "denied")
    finally:
        events.pop(key, None)
        decisions.pop(key, None)

    return decision


async def resolve(approval_id, decision: str) -> bool:
    """Record a decision, update the row, and wake the waiting request."""
    if decision not in ("approved", "denied"):
        raise ValueError(f"bad decision: {decision}")

    key = str(approval_id)
    updated = await _record(key, decision)
    if not updated:
        return False

    decisions[key] = decision
    event = events.get(key)
    if event:
        event.set()
    return True


async def list_pending() -> list[dict]:
    async with Session() as session:
        result = await session.execute(
            select(PendingApproval)
            .where(PendingApproval.status == "pending")
            .order_by(PendingApproval.created_at)
        )
        return [row.as_dict() for row in result.scalars()]


async def _record(key: str, status: str) -> bool:
    """Move a still-pending row to `status`. False if it was already decided."""
    async with Session() as session:
        row = await session.get(PendingApproval, uuid.UUID(key))
        if row is None or row.status != "pending":
            return False
        row.status = status
        row.decided_at = datetime.now(timezone.utc)
        await session.commit()
    return True
