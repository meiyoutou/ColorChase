"""检查/补齐存储配额表字段（默认 dry-run，传 --apply 才修改 MySQL）。

create_all 只会创建新表，不会给中间开发版本已经创建的表增加新列。本脚本用于
部署前检查 user_storage_quotas / storage_quota_reservations 是否与当前 models.py
一致。默认只打印 SQL；禁止对生产库直接盲目执行 --apply。
"""
import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[3]


USER_COLUMNS = {
    "reconciled_at": "DATETIME NULL",
    "reconcile_needed": "BOOLEAN NOT NULL DEFAULT 0",
    "reconcile_reason": "VARCHAR(128) NULL",
    "reconcile_sample_key": "VARCHAR(256) NULL",
    "reconcile_marked_at": "DATETIME NULL",
    "would_deny_count": "INT NOT NULL DEFAULT 0",
    "denied_count": "INT NOT NULL DEFAULT 0",
    "last_would_deny_at": "DATETIME NULL",
    "last_denied_at": "DATETIME NULL",
}
RESERVATION_COLUMNS = {
    "expires_at": "DATETIME NULL",
}
RESERVATION_INDEXES = {
    "ix_reservation_status_expires": (
        "CREATE INDEX `ix_reservation_status_expires` "
        "ON `storage_quota_reservations` (`status`, `expires_at`)"
    ),
}


def _load_url():
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("必须配置 COLORCHASE_DATABASE_URL")
    if url.startswith("mysql+pymysql://"):
        url = "mysql+aiomysql://" + url.split("://", 1)[1]
    if not url.startswith("mysql+aiomysql://"):
        raise RuntimeError("只支持 MySQL URL")
    return url


async def _columns(conn, table):
    rows = await conn.execute(
        text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table"
        ),
        {"table": table},
    )
    return {row[0] for row in rows.all()}


async def _indexes(conn, table):
    rows = await conn.execute(
        text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table"
        ),
        {"table": table},
    )
    return {row[0] for row in rows.all()}


async def main(apply=False):
    engine = create_async_engine(_load_url(), pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            changes = []
            existing_tables = set()
            for table, expected in (
                ("user_storage_quotas", USER_COLUMNS),
                ("storage_quota_reservations", RESERVATION_COLUMNS),
            ):
                existing = await _columns(conn, table)
                if not existing:
                    print(f"[skip] {table} 不存在；先运行应用 init_db/create_all")
                    continue
                existing_tables.add(table)
                for column, ddl in expected.items():
                    if column not in existing:
                        changes.append(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}")
            if "storage_quota_reservations" in existing_tables:
                reservation_indexes = await _indexes(conn, "storage_quota_reservations")
                for name, ddl in RESERVATION_INDEXES.items():
                    if name not in reservation_indexes:
                        changes.append(ddl)
            if not changes:
                print("存储配额 schema 已是最新，无需修改。")
                return
            for sql in changes:
                print(("[apply] " if apply else "[dry-run] ") + sql)
                if apply:
                    await conn.execute(text(sql))
            if not apply:
                print("未修改数据库；确认后加 --apply 执行。")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际执行 ALTER TABLE")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
