# MCP Tool Gateway

A minimal control plane between AI agents and organizational tools, with a
human-in-the-loop approval gate for high-risk actions.

Low-risk tools (read-only SQL) run instantly. High-risk tools (sending email)
pause and wait for a human to approve or deny from an admin dashboard before the
agent is allowed to proceed. Every call is written to an audit log.

**Design goal:** keep it linear and synchronous. The approval "pause" is a plain
`await` on an in-memory `asyncio.Event` under a timeout — not a distributed
pause/resume state machine. No Docker sandbox, no arbitrary code execution, no
multi-tenancy, no Redis. Those are deliberate v1 exclusions.

## How it works

```
Agent --POST /call--> Gateway
   low risk  -> run tool -> audit(auto)     -> result
   high risk -> PendingApproval(pending)
                await decision (APPROVAL_TIMEOUT)
                  approved -> run tool -> audit(approved) -> result
                  denied   ->             audit(denied)   -> {"status":"denied"}
                  timeout  ->             audit(timeout)  -> {"status":"timeout"}
```

The core is one function, `gateway.run_tool`: check the tool's risk flag, then
either run it now or create a pending row and await a decision.

## Tech stack

FastAPI · SQLAlchemy (async) + asyncpg · PostgreSQL · `mcp` SDK (Streamable HTTP) · uvicorn

## File layout

```
mcp-gateway/
├── app/
│   ├── main.py         # FastAPI app + routes wired together
│   ├── db.py           # Postgres connection/session (rw + ro engines)
│   ├── models.py       # PendingApproval, AuditLog
│   ├── registry.py     # tool name -> {fn, risk}
│   ├── tools.py        # sql_read (low), send_email (high)
│   ├── gateway.py      # core: run a tool + enforce the gate
│   ├── approvals.py    # in-memory events + wait/approve/deny
│   └── mcp_server.py   # Step 6: registry -> MCP tools, mounted at /mcp
├── static/
│   └── dashboard.html  # admin page (polls for pending requests every 2s)
├── .env
├── requirements.txt
└── README.md
```

## Setup

Prerequisites: Python 3.11+, PostgreSQL running locally.

**Install**

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Configure** — `.env` (already present):

```
DATABASE_URL=postgresql+asyncpg://gateway:password@localhost:5432/mcp_gateway
READONLY_DATABASE_URL=postgresql+asyncpg://gateway_ro:password@localhost:5432/mcp_gateway
APPROVAL_TIMEOUT=300
```

**Create the database and the read-only role.** `sql_read` uses `gateway_ro` —
SELECT-only is enforced by the database and by a `READ ONLY` transaction, never
by parsing the query string.

*Option A — Docker (what this checkout is set up with):*

```bash
docker run -d --name mcp_gateway_postgres \
  -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=password -e POSTGRES_DB=mcp_gateway \
  -p 127.0.0.1:5432:5432 postgres:15-alpine

docker exec -i mcp_gateway_postgres psql -U gateway -d mcp_gateway <<'SQL'
CREATE ROLE gateway_ro LOGIN PASSWORD 'password';
GRANT CONNECT ON DATABASE mcp_gateway TO gateway_ro;
GRANT USAGE ON SCHEMA public TO gateway_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gateway_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gateway_ro;
SQL
```

Stop/start it later with `docker stop mcp_gateway_postgres` / `docker start mcp_gateway_postgres`.

*Option B — local Postgres install:*

```bash
createdb mcp_gateway
psql mcp_gateway -c "CREATE ROLE gateway_ro LOGIN PASSWORD 'password';
  GRANT CONNECT ON DATABASE mcp_gateway TO gateway_ro;
  GRANT USAGE ON SCHEMA public TO gateway_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO gateway_ro;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gateway_ro;"
```

The `ALTER DEFAULT PRIVILEGES` line matters: the gateway creates its tables on
startup, *after* the `GRANT ... ON ALL TABLES` above has already run, so without
it `gateway_ro` would be granted nothing.

**Run** — from inside `mcp-gateway/`, not your home directory. `app.main:app` is
resolved relative to the current directory:

```bash
cd mcp-gateway
.venv/bin/uvicorn app.main:app --reload      # or: source .venv/bin/activate first
```

> If you see `externally-managed-environment` from pip, the venv isn't active —
> use `.venv/bin/pip` explicitly. If you see `ModuleNotFoundError: No module
> named 'app'`, you're in the wrong directory.

Dashboard: <http://localhost:8000/> · API: <http://localhost:8000/call>

## Build order

Each step runs before you start the next.

- [x] **1. Skeleton + DB** — FastAPI app, async SQLAlchemy engine, `AuditLog` + `PendingApproval` created on startup.
- [x] **2. Registry + tools** — `sql_read` (risk `low`) and `send_email` (risk `high`) registered by name.
- [x] **3. Gateway + gate** — `run_tool()` runs low-risk now, blocks high-risk; exposed via `POST /call`.
- [x] **4. Approval store** — in-memory `asyncio.Event` per pending id; `wait_for_decision()` / `resolve()`.
- [x] **5. Dashboard** — `GET /approvals`, approve/deny endpoints, `dashboard.html` polling every 2s.
- [x] **6. MCP endpoint** — the registry exposed over MCP via the `mcp` SDK (Streamable HTTP) at `/mcp`.

## API

| Method | Path                        | Purpose                          |
|--------|-----------------------------|----------------------------------|
| POST   | `/call`                     | `{tool, args}` → runs the gateway |
| GET    | `/approvals`                | List pending approvals (dashboard feed) |
| POST   | `/approvals/{id}/approve`   | Approve a pending call           |
| POST   | `/approvals/{id}/deny`      | Deny a pending call              |
| GET    | `/health`                   | Liveness + registered tool names |
| GET    | `/`                         | Admin dashboard                  |
| ALL    | `/mcp`                      | MCP endpoint (Streamable HTTP)   |

## Demo / acceptance test

Low-risk tool → returns instantly, `AuditLog.decision = "auto"`:

```bash
curl -X POST localhost:8000/call -H "content-type: application/json" \
  -d '{"tool":"sql_read","args":{"query":"select 1 as ok"}}'
```

High-risk tool → the request hangs:

```bash
curl -X POST localhost:8000/call -H "content-type: application/json" \
  -d '{"tool":"send_email","args":{"to":"a@b.com","subject":"hi","body":"test"}}'
```

Open <http://localhost:8000/> → the request is listed → click **Approve** → the
curl call returns its result and `AuditLog.decision = "approved"`. Repeat with
**Deny**, and leave one sitting for `APPROVAL_TIMEOUT` seconds to see the
timeout path.

```sql
SELECT tool_name, risk, decision, created_at FROM audit_log ORDER BY id DESC LIMIT 5;
```

## Notes and known edges

- **Single process only.** The event lives in one worker's memory, so don't run
  `--workers 2` — the approver and the waiter must share an event loop. Redis
  Pub/Sub is the fix when that changes.
- **Restart loses waiters.** A pending row survives a restart, but the request
  awaiting it does not. Resolving an orphaned row updates the DB and returns
  cleanly; nothing is left hanging.
- **Timeout closes the row.** On timeout the `PendingApproval` row is marked
  `denied` (the schema only has `pending|approved|denied`) so it leaves the
  dashboard, while the `AuditLog` row records the more precise `decision="timeout"`.
- **Failed tools are audited too.** If a tool raises (bad args, or the DB
  refusing a write), the gateway writes an `AuditLog` row with
  `decision="error"` and returns HTTP 400 with the message — a refused call is
  exactly what the audit log is for.
- **No auth on the dashboard.** Anyone who can reach the port can approve. Put
  it behind your own auth before it leaves localhost.

## MCP endpoint

The same registry is served over MCP at `http://localhost:8000/mcp` (Streamable
HTTP). Each MCP tool handler is a thin wrapper around `gateway.run_tool`, so the
approval gate applies automatically — an agent calling `send_email` simply sees a
tool that takes a while to return.

Connect MCP Inspector:

```bash
npx @modelcontextprotocol/inspector
# transport: Streamable HTTP   URL: http://localhost:8000/mcp
```

Initialize → `list tools` shows `sql_read` and `send_email` → calling `sql_read`
returns rows immediately → calling `send_email` hangs until you click Approve on
the dashboard.

**SDK note:** this uses `mcp` **2.x**, where `FastMCP` was renamed to
`MCPServer` (`from mcp.server.mcpserver import MCPServer`) and the wire models
moved to snake_case (`result.is_error`, `tool.input_schema`). Most tutorials
online still show the v1 names. The handler signatures are generated from the
real tool functions via `functools.wraps` + `__signature__`, so the JSON schema
stays in sync with `tools.py` automatically.

A denied call returns `{"status": "denied"}` as a **successful** tool result, not
an MCP error — the agent is told the action was refused rather than that
something broke. Flip this in `gateway.run_tool` if you'd rather it raise.

## Out of scope (v1, on purpose)

| Excluded | Why / when to add |
|---|---|
| Docker sandbox | Real isolation is hard (gVisor/Firecracker territory); add only with arbitrary code exec. |
| Arbitrary code exec | Highest-risk, easiest to do insecurely — keep to a fixed tool set first. |
| Multi-tenancy | Add a `tenant_id` column when needed; don't build isolation machinery yet. |
| Redis Pub/Sub | Only needed when approver and waiter live in different processes. |

## Roadmap

1. Approval token expiry + idempotency keys.
2. WebSocket push to the dashboard (replace polling).
3. Redis Pub/Sub so the gate works across multiple workers.
4. Sandboxed code-execution tool with real isolation (gVisor/Firecracker, no network, non-root).
5. Per-tenant policies and role-based approvals.
