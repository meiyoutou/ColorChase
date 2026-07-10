"""Add users.storage_label for stable per-user storage directories.

Run with COLORCHASE_DATABASE_URL set to a MySQL URL. This migration is
idempotent and does not touch any storage files.
"""
import asyncio
import os

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine


COLUMN_SQL = """
SELECT COUNT(*)
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'users'
  AND COLUMN_NAME = 'storage_label'
"""

INDEX_SQL = """
SELECT COUNT(*)
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'users'
  AND INDEX_NAME = 'ux_users_storage_label'
"""


def _database_url() -> str:
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("必须配置 COLORCHASE_DATABASE_URL")
    if not url.startswith(("mysql+aiomysql://", "mysql+pymysql://")):
        raise RuntimeError("本迁移只支持 mysql+aiomysql:// 或 mysql+pymysql://")
    return url


async def _run_async(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            column_exists = (await conn.execute(text(COLUMN_SQL))).scalar_one()
            if not column_exists:
                await conn.execute(text("ALTER TABLE users ADD COLUMN storage_label VARCHAR(128) NULL"))

            index_exists = (await conn.execute(text(INDEX_SQL))).scalar_one()
            if not index_exists:
                await conn.execute(text("CREATE UNIQUE INDEX ux_users_storage_label ON users(storage_label)"))
    finally:
        await engine.dispose()


def _run_sync(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            column_exists = conn.execute(text(COLUMN_SQL)).scalar_one()
            if not column_exists:
                conn.execute(text("ALTER TABLE users ADD COLUMN storage_label VARCHAR(128) NULL"))

            index_exists = conn.execute(text(INDEX_SQL)).scalar_one()
            if not index_exists:
                conn.execute(text("CREATE UNIQUE INDEX ux_users_storage_label ON users(storage_label)"))
    finally:
        engine.dispose()


def main() -> None:
    url = _database_url()
    if url.startswith("mysql+aiomysql://"):
        asyncio.run(_run_async(url))
    else:
        _run_sync(url)
    print("users.storage_label migration is up to date")


if __name__ == "__main__":
    main()
