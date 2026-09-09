"""The tool implementations.

sql_read   -- low risk:  runs a SELECT as the read-only DB role.
send_email -- high risk: logs the email for now (stand-in for a real sender).
"""
import logging
import os

from sqlalchemy import text

from app.db import ro_engine

log = logging.getLogger("gateway.tools")

MAX_ROWS = int(os.getenv("SQL_READ_MAX_ROWS", "200"))


async def sql_read(query: str) -> dict:
    """Run a read-only query.

    Safety comes from the database, not from inspecting `query`:
      * the connection uses the gateway_ro role (SELECT grants only)
      * the transaction is READ ONLY, so writes error out
      * statement_timeout kills long queries
      * we stop reading after MAX_ROWS rows
    """
    async with ro_engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        result = await conn.execute(text(query))
        if not result.returns_rows:
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

        columns = list(result.keys())
        rows = result.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]

    return {
        "columns": columns,
        "rows": [[_jsonable(v) for v in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


async def send_email(to: str, subject: str, body: str) -> dict:
    """Pretend to send an email. Swap the log line for a real provider later."""
    log.info("SEND EMAIL to=%s subject=%r body=%r", to, subject, body)
    return {"sent": True, "to": to, "subject": subject}


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
