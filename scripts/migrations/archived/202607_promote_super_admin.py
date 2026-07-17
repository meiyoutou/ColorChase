"""Promote one existing user to super_admin.

Default target account is "11". It can match user id, email, phone, or
storage_label. Override with COLORCHASE_SUPER_ADMIN_ACCOUNT or the stricter
COLORCHASE_SUPER_ADMIN_USER_ID when a deployment uses a different owner account.
"""
import asyncio
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine


def _load_local_env_defaults() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
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


def _target_selector() -> tuple[str, str | int]:
    raw_id = os.environ.get("COLORCHASE_SUPER_ADMIN_USER_ID", "").strip()
    if raw_id:
        raw = raw_id
        selector = "id"
    else:
        raw = os.environ.get("COLORCHASE_SUPER_ADMIN_ACCOUNT", "11").strip()
        selector = "account"
    if not raw:
        raise RuntimeError("必须配置超级管理员账号标识")
    if selector == "account":
        return selector, raw
    try:
        user_id = int(raw)
    except ValueError as exc:
        raise RuntimeError("COLORCHASE_SUPER_ADMIN_USER_ID 必须是数字") from exc
    if user_id <= 0:
        raise RuntimeError("COLORCHASE_SUPER_ADMIN_USER_ID 必须大于 0")
    return selector, user_id


def _find_user_sql(selector: str) -> str:
    if selector == "id":
        return "SELECT id, email, phone, role, storage_label FROM users WHERE id = :id"
    return """
SELECT id, email, phone, role, storage_label
FROM users
WHERE CAST(id AS CHAR) = :account
   OR email = :account
   OR phone = :account
   OR storage_label = :account
   OR storage_label = :storage_label
"""


def _find_user_params(selector: str, target: str | int) -> dict:
    if selector == "id":
        return {"id": target}
    account = str(target)
    storage_label = account if account.startswith("user_") else f"user_{account}"
    return {"account": account, "storage_label": storage_label}


async def _run_async(url: str, selector: str, target: str | int) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(text(_find_user_sql(selector)), _find_user_params(selector, target))
            ).mappings().all()
            user_ids = {int(row["id"]) for row in rows}
            if not rows:
                raise RuntimeError(f"账号 {target} 不存在，无法设置超级管理员")
            if len(user_ids) > 1:
                raise RuntimeError(f"账号 {target} 匹配到多个用户，请改用 COLORCHASE_SUPER_ADMIN_USER_ID")
            row = rows[0]
            user_id = int(row["id"])
            await conn.execute(
                text("UPDATE users SET role = 'super_admin' WHERE id = :id"),
                {"id": user_id},
            )
            account = row.get("email") or row.get("phone") or f"user_{user_id}"
            return f"账号 {target} 对应用户 {user_id} ({account})，已设置为 super_admin"
    finally:
        await engine.dispose()


def _run_sync(url: str, selector: str, target: str | int) -> str:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(_find_user_sql(selector)),
                _find_user_params(selector, target),
            ).mappings().all()
            user_ids = {int(row["id"]) for row in rows}
            if not rows:
                raise RuntimeError(f"账号 {target} 不存在，无法设置超级管理员")
            if len(user_ids) > 1:
                raise RuntimeError(f"账号 {target} 匹配到多个用户，请改用 COLORCHASE_SUPER_ADMIN_USER_ID")
            row = rows[0]
            user_id = int(row["id"])
            conn.execute(
                text("UPDATE users SET role = 'super_admin' WHERE id = :id"),
                {"id": user_id},
            )
            account = row.get("email") or row.get("phone") or f"user_{user_id}"
            return f"账号 {target} 对应用户 {user_id} ({account})，已设置为 super_admin"
    finally:
        engine.dispose()


def main() -> None:
    url = _database_url()
    selector, target = _target_selector()
    if url.startswith("mysql+aiomysql://"):
        message = asyncio.run(_run_async(url, selector, target))
    else:
        message = _run_sync(url, selector, target)
    print(message)


if __name__ == "__main__":
    main()
