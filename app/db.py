"""Postgres connection/session.

Two engines on purpose:
  engine    -- full-privilege, used for the gateway's own tables
  ro_engine -- the gateway_ro role, used only by the sql_read tool.
               SELECT-only is enforced by the database, never by parsing SQL.
"""
import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
READONLY_DATABASE_URL = os.getenv("READONLY_DATABASE_URL", DATABASE_URL)
STATEMENT_TIMEOUT_MS = os.getenv("STATEMENT_TIMEOUT_MS", "5000")


class Base(DeclarativeBase):
    pass


engine = create_async_engine(DATABASE_URL, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# statement_timeout is set on the connection itself, so a runaway query in
# sql_read is killed by Postgres rather than by us.
ro_engine = create_async_engine(
    READONLY_DATABASE_URL,
    echo=False,
    connect_args={"server_settings": {"statement_timeout": STATEMENT_TIMEOUT_MS}},
)


async def create_tables() -> None:
    from app import models  # noqa: F401  (register mappers before create_all)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
