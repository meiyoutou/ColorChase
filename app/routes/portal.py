import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routes.auth import require_admin
from config import BASE_DIR, get_storage_cache_dir

router = APIRouter()

PORTAL_MESSAGES_PATH = get_storage_cache_dir() / "portal_messages.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state():
    now = _now_iso()
    return {
        "notice": {
            "version": 1,
            "title": "更新日志",
            "body": "部署后更新日志将在这里展示。\n可用于放置版本更新、功能上线说明、修复记录与维护通知。",
            "items": [
                {
                    "id": uuid.uuid4().hex,
                    "title": "更新日志",
                    "body": "部署后更新日志将在这里展示。\n可用于放置版本更新、功能上线说明、修复记录与维护通知。",
                    "created_at": now,
                }
            ],
            "updated_at": now,
        },
        "contact": {
            "version": 1,
            "qq": "955749464",
            "notes": "沟通QQ群：955749464",
            "updated_at": now,
        },
    }


def _normalize_state(state):
    base = _default_state()
    if not isinstance(state, dict):
        return base

    notice = state.get("notice") if isinstance(state.get("notice"), dict) else {}
    contact = state.get("contact") if isinstance(state.get("contact"), dict) else {}

    items = notice.get("items")
    if not isinstance(items, list) or not items:
        items = base["notice"]["items"]

    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip() or uuid.uuid4().hex
        title = str(item.get("title") or "").strip() or "新消息"
        body = str(item.get("body") or "").strip()
        created_at = str(item.get("created_at") or _now_iso()).strip()
        normalized_items.append({
            "id": item_id,
            "title": title,
            "body": body,
            "created_at": created_at,
        })

    if not normalized_items:
        normalized_items = base["notice"]["items"]

    notice_version = int(notice.get("version") or 1)
    contact_version = int(contact.get("version") or 1)

    return {
        "notice": {
            "version": max(1, notice_version),
            "title": str(notice.get("title") or base["notice"]["title"]).strip() or base["notice"]["title"],
            "body": str(notice.get("body") or base["notice"]["body"]).strip() or base["notice"]["body"],
            "items": normalized_items,
            "updated_at": str(notice.get("updated_at") or _now_iso()).strip(),
        },
        "contact": {
            "version": max(1, contact_version),
            "qq": str(contact.get("qq") or base["contact"]["qq"]).strip() or base["contact"]["qq"],
            "notes": str(contact.get("notes") or base["contact"]["notes"]).strip() or base["contact"]["notes"],
            "updated_at": str(contact.get("updated_at") or _now_iso()).strip(),
        },
    }


def _read_state():
    if not PORTAL_MESSAGES_PATH.exists():
        return _default_state()
    try:
        with open(PORTAL_MESSAGES_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        normalized = _normalize_state(payload)
        if normalized != payload:
            _write_state(normalized)
        return normalized
    except Exception:
        return _default_state()


def _write_state(state):
    PORTAL_MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PORTAL_MESSAGES_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class PortalMessageUpdate(BaseModel):
    notice_title: str | None = None
    notice_body: str | None = None
    contact_qq: str | None = None
    contact_notes: str | None = None


class PortalNoticeDeleteRequest(BaseModel):
    ids: list[str]


@router.get("/portal_messages")
async def read_portal_messages():
    state = _read_state()
    return {
        "notice": state["notice"],
        "contact": state["contact"],
        "meta": {
            "auto_refresh_seconds": 60,
            "generated_at": _now_iso(),
        },
    }


@router.post("/admin/portal_messages")
async def update_portal_messages(payload: PortalMessageUpdate, _admin=Depends(require_admin)):
    state = _read_state()
    changed = False
    now = _now_iso()

    notice_title = (payload.notice_title or "").strip()
    notice_body = (payload.notice_body or "").strip()
    if notice_title or notice_body:
        changed = True
        current_notice = state["notice"]
        current_notice["version"] = int(current_notice.get("version") or 1) + 1
        current_notice["title"] = notice_title or "新消息"
        current_notice["body"] = notice_body
        current_notice["updated_at"] = now
        items = list(current_notice.get("items") or [])
        items.insert(0, {
            "id": uuid.uuid4().hex,
            "title": current_notice["title"],
            "body": notice_body,
            "created_at": now,
        })
        current_notice["items"] = items[:10]

    contact_qq = (payload.contact_qq or "").strip()
    contact_notes = (payload.contact_notes or "").strip()
    if contact_qq or contact_notes:
        changed = True
        current_contact = state["contact"]
        current_contact["version"] = int(current_contact.get("version") or 1) + 1
        if contact_qq:
            current_contact["qq"] = contact_qq
        if contact_notes:
            current_contact["notes"] = contact_notes
        current_contact["updated_at"] = now

    if not changed:
        raise HTTPException(status_code=400, detail="未提供可更新内容")

    state = _normalize_state(state)
    _write_state(state)
    return {
        "notice": state["notice"],
        "contact": state["contact"],
        "meta": {
            "auto_refresh_seconds": 60,
            "generated_at": now,
        },
    }


@router.post("/admin/portal_messages/delete")
async def delete_portal_notice_items(payload: PortalNoticeDeleteRequest, _admin=Depends(require_admin)):
    ids = [str(item_id or "").strip() for item_id in (payload.ids or []) if str(item_id or "").strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="未选择要删除的更新日志")

    state = _read_state()
    current_notice = state["notice"]
    items = list(current_notice.get("items") or [])
    delete_ids = set(ids)
    kept_items = []
    for index, item in enumerate(items):
        item_id = str(item.get("id") or "").strip()
        legacy_id = f"legacy-{index}"
        if item_id in delete_ids or legacy_id in delete_ids:
            continue
        kept_items.append(item)

    if len(kept_items) == len(items):
        raise HTTPException(status_code=404, detail="未找到可删除的更新日志")
    if not kept_items:
        raise HTTPException(status_code=400, detail="至少保留一条更新日志")

    current_notice["items"] = kept_items[:10]
    current_notice["title"] = str(kept_items[0].get("title") or "更新日志").strip() or "更新日志"
    current_notice["body"] = str(kept_items[0].get("body") or "").strip()
    current_notice["updated_at"] = _now_iso()
    current_notice["version"] = int(current_notice.get("version") or 1) + 1

    state = _normalize_state(state)
    _write_state(state)
    refreshed = _read_state()
    return {
        "notice": refreshed["notice"],
        "contact": refreshed["contact"],
        "meta": {
            "auto_refresh_seconds": 60,
            "generated_at": current_notice["updated_at"],
        },
    }
