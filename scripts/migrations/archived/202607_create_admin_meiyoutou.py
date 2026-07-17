"""Create admin account meiyoutou.

Run with COLORCHASE_DATABASE_URL set to a MySQL URL.
Idempotent: if the account already exists, only role/password will be updated.
"""
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auth import get_password_hash  # noqa: E402


ADMIN_PHONE = "meiyoutou"
ADMIN_PASSWORD = "tao040721."
ADMIN_ROLE = "admin"


def _load_local_env_defaults() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    except OSError:
        return


def _database_url() -> str:
    _load_local_env_defaults()
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("必须配置 COLORCHASE_DATABASE_URL")
    if not url.startswith(("mysql+aiomysql://", "mysql+pymysql://")):
        raise RuntimeError("本迁移只支持 mysql+aiomysql:// 或 mysql+pymysql://")
    return url


async def _run_async(url: str) -> str:
    engine = create_async_engine(url)
    hashed = get_password_hash(ADMIN_PASSWORD)
    storage_label = f"user_{ADMIN_PHONE}"
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id, phone, role FROM users WHERE phone = :phone"),
                    {"phone": ADMIN_PHONE},
                )
            ).mappings().all()
            if rows:
                user_id = int(rows[0]["id"])
                await conn.execute(
                    text(
                        "UPDATE users SET hashed_password = :pwd, role = :role, "
                        "storage_label = :label WHERE id = :id"
                    ),
                    {
                        "pwd": hashed,
                        "role": ADMIN_ROLE,
                        "label": storage_label,
                        "id": user_id,
                    },
                )
                return f"账号 {ADMIN_PHONE} 已存在，已重置密码并设置为 {ADMIN_ROLE}"

            await conn.execute(
                text(
                    "INSERT INTO users (phone, hashed_password, role, storage_label) "
                    "VALUES (:phone, :pwd, :role, :label)"
                ),
                {
                    "phone": ADMIN_PHONE,
                    "pwd": hashed,
                    "role": ADMIN_ROLE,
                    "label": storage_label,
                },
            )
            return f"账号 {ADMIN_PHONE} 已创建，角色 {ADMIN_ROLE}"
    finally:
        await engine.dispose()


def _run_sync(url: str) -> str:
    engine = create_engine(url)
    hashed = get_password_hash(ADMIN_PASSWORD)
    storage_label = f"user_{ADMIN_PHONE}"
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, phone, role FROM users WHERE phone = :phone"),
                {"phone": ADMIN_PHONE},
            ).mappings().all()
            if rows:
                user_id = int(rows[0]["id"])
                conn.execute(
                    text(
                        "UPDATE users SET hashed_password = :pwd, role = :role, "
                        "storage_label = :label WHERE id = :id"
                    ),
                    {
                        "pwd": hashed,
                        "role": ADMIN_ROLE,
                        "label": storage_label,
                        "id": user_id,
                    },
                )
                return f"账号 {ADMIN_PHONE} 已存在，已重置密码并设置为 {ADMIN_ROLE}"

            conn.execute(
                text(
                    "INSERT INTO users (phone, hashed_password, role, storage_label) "
                    "VALUES (:phone, :pwd, :role, :label)"
                ),
                {
                    "phone": ADMIN_PHONE,
                    "pwd": hashed,
                    "role": ADMIN_ROLE,
                    "label": storage_label,
                },
            )
            return f"账号 {ADMIN_PHONE} 已创建，角色 {ADMIN_ROLE}"
    finally:
        engine.dispose()


def main() -> None:
    url = _database_url()
    if url.startswith("mysql+aiomysql://"):
        message = asyncio.run(_run_async(url))
    else:
        message = _run_sync(url)
    # 2026-07-16 调试：trae-sandbox 下 stdout 可能被截断，写文件兜底确认。
    result_path = ROOT / "meiyoutou_migrate.log"
    result_path.write_text(message + "\n", encoding="utf-8")
    print(message)


if __name__ == "__main__":
    main()
