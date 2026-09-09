"""FastAPI app + routes wired together."""
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import approvals, gateway, registry
from app.db import create_tables
from app.mcp_server import mcp, mcp_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    # A mounted sub-app's own lifespan never runs, so drive the MCP session
    # manager from the parent app's.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="MCP Tool Gateway", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/mcp", mcp_app)  # Step 6: MCP over Streamable HTTP


class CallRequest(BaseModel):
    tool: str
    args: dict = {}


@app.get("/health")
async def health():
    return {"status": "ok", "tools": sorted(registry.REGISTRY)}


@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.post("/call")
async def call(req: CallRequest):
    """Low-risk tools return immediately; high-risk ones block until decided."""
    try:
        return await gateway.run_tool(req.tool, req.args)
    except gateway.UnknownTool:
        raise HTTPException(status_code=404, detail=f"unknown tool: {req.tool}")
    except gateway.ToolError as exc:  # bad args, or the DB refused the query
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/approvals")
async def list_approvals():
    return await approvals.list_pending()


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: uuid.UUID):
    return await _decide(approval_id, "approved")


@app.post("/approvals/{approval_id}/deny")
async def deny(approval_id: uuid.UUID):
    return await _decide(approval_id, "denied")


async def _decide(approval_id: uuid.UUID, decision: str):
    if not await approvals.resolve(approval_id, decision):
        raise HTTPException(status_code=404, detail="not pending (unknown or already decided)")
    return {"id": str(approval_id), "status": decision}
