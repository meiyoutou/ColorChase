import re
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select, text

from database import async_session
from models import User


_STORAGE_LABEL_CACHE: dict[int, str] = {}
_ALLOWED_LABEL_CHARS = re.compile(r"[^A-Za-z0-9@._-]+")
_MAX_STORAGE_LABEL_LENGTH = 128


def clear_user_storage_label_cache(user_id: Optional[int] = None) -> None:
    if user_id is None:
        _STORAGE_LABEL_CACHE.clear()
        return
    _STORAGE_LABEL_CACHE.pop(int(user_id), None)


def build_user_storage_label(identity: str) -> str:
    raw = str(identity or "").strip().lower()
    safe = _ALLOWED_LABEL_CHARS.sub("_", raw)
    return ("user_" + safe)[:_MAX_STORAGE_LABEL_LENGTH]


def _row_value(row: Any, name: str, index: int):
    if row is None:
        return None
    if hasattr(row, name):
        return getattr(row, name)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and name in mapping:
        return mapping[name]
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


async def _read_user_row(session, user_id: int):
    result = await session.execute(
        select(User.id, User.email, User.phone, User.storage_label).where(User.id == user_id)
    )
    return result.first()


async def _read_storage_label(session, user_id: int) -> Optional[str]:
    result = await session.execute(select(User.storage_label).where(User.id == user_id))
    label = result.scalar_one_or_none()
    return str(label).strip() if label else None


async def resolve_user_storage_label(user_id: int) -> str:
    uid = int(user_id)
    cached = _STORAGE_LABEL_CACHE.get(uid)
    if cached:
        return cached

    async with async_session() as session:
        row = await _read_user_row(session, uid)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        existing = _row_value(row, "storage_label", 3)
        if existing:
            label = str(existing).strip()
            _STORAGE_LABEL_CACHE[uid] = label
            return label

        email = str(_row_value(row, "email", 1) or "").strip()
        phone = str(_row_value(row, "phone", 2) or "").strip()
        identity = email or phone
        if not identity:
            raise HTTPException(status_code=400, detail="用户缺少邮箱或手机号，无法创建用户目录")

        generated = build_user_storage_label(identity)
        await session.execute(
            text("UPDATE users SET storage_label = :label WHERE id = :id AND storage_label IS NULL"),
            {"label": generated, "id": uid},
        )
        await session.commit()

        label = await _read_storage_label(session, uid)
        if not label:
            raise RuntimeError("写回 users.storage_label 后重新读取失败")

        _STORAGE_LABEL_CACHE[uid] = label
        return label
