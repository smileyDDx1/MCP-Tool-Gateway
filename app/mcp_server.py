"""Step 6 — expose the registry over MCP (Streamable HTTP).

Every MCP tool handler is a thin wrapper around gateway.run_tool, so the
human-in-the-loop gate applies automatically: a high-risk call blocks until a
human approves it in the dashboard, and the agent just sees a slow tool.

Note the SDK naming: mcp>=2 renamed FastMCP to MCPServer.
"""
import functools
import inspect
import logging

from mcp.server.mcpserver import MCPServer

from app import gateway, registry

log = logging.getLogger("gateway.mcp")

mcp = MCPServer(
    name="mcp-tool-gateway",
    version="1.0.0",
    instructions=(
        "Tools proxied through a human-in-the-loop gateway. Low-risk tools return "
        "immediately; high-risk tools block until a human approves the call, so a "
        "slow response means someone is reviewing it, not that the server hung."
    ),
)


def _make_handler(name: str, entry: registry.ToolEntry):
    """Wrap a registry tool so MCP calls go through the gate.

    functools.wraps copies __wrapped__ and __annotations__, so the SDK's schema
    generation sees the real signature (e.g. query: str) instead of **kwargs.
    """

    @functools.wraps(entry.fn)
    async def handler(**kwargs):
        return await gateway.run_tool(name, kwargs)

    handler.__signature__ = inspect.signature(entry.fn)
    return handler


for _name, _entry in registry.REGISTRY.items():
    mcp.add_tool(
        _make_handler(_name, _entry),
        name=_name,
        description=f"{_entry.description} (risk: {_entry.risk}"
        + (" — blocks for human approval)" if _entry.risk != "low" else ")"),
    )
    log.info("registered MCP tool %s (risk=%s)", _name, _entry.risk)

# Built at import time so `mcp.session_manager` exists for the FastAPI lifespan.
# streamable_http_path="/" because FastAPI mounts this whole app under /mcp.
mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True)
