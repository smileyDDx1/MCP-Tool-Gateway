"""tool name -> {fn, risk}."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app import tools


@dataclass(frozen=True)
class ToolEntry:
    fn: Callable[..., Awaitable[Any]]
    risk: str  # "low" runs immediately; anything else needs human approval
    description: str


REGISTRY: dict[str, ToolEntry] = {
    "sql_read": ToolEntry(
        fn=tools.sql_read,
        risk="low",
        description="Run a read-only SQL query. args: {query}",
    ),
    "send_email": ToolEntry(
        fn=tools.send_email,
        risk="high",
        description="Send an email. args: {to, subject, body}",
    ),
}


def get(name: str) -> ToolEntry | None:
    return REGISTRY.get(name)
