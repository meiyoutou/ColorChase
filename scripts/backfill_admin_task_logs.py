import asyncio
import json
import re
import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select

from database import async_session
from models import User
from app.routes.projects import _derive_display_name, _user_profile_record


RUNTIME_STATS_PATH = BASE_DIR / ".cache" / "admin_runtime_metrics.json"
TASK_LOG_USER_KEY_PATTERN = re.compile(r"user_(\d+)_(?:admin|user)$", re.IGNORECASE)


def _format_meta_value(key: str, value):
    if value is None or value == "":
        return "--"
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if key in {"training_size_mb", "export_size_mb"}:
        try:
            return f"{float(value):.1f} MB"
        except (TypeError, ValueError):
            return f"{value} MB"
    if key == "export_size_bytes":
        try:
            size = float(value)
        except (TypeError, ValueError):
            return str(value)
        if size >= 1024 * 1024:
            return f"{size / 1024 / 1024:.2f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{int(size)} B"
    if key == "source":
        return {
            "frontend_export": "前端导出",
            "apply_profile": "配置应用",
            "apply_style": "风格应用",
        }.get(str(value), str(value))
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _build_meta_display(meta: dict):
    if not isinstance(meta, dict):
        return []
    label_map = {
        "enable_metrics": "质量评估",
        "enable_postprocess": "智能后处理",
        "enable_scene_detect": "场景检测",
        "stage": "训练阶段",
        "epochs": "Epoch",
        "batch_size": "Batch Size",
        "lr": "学习率",
        "training_file_count": "训练图数",
        "training_size_mb": "训练数据量",
        "export_format": "导出格式",
        "size_mode": "尺寸模式",
        "export_size_bytes": "导出体积",
        "export_file_count": "导出文件数",
        "project_id": "项目编号",
        "file_name": "文件名",
        "source_image_key": "源图标识",
        "source": "来源",
        "user_email": "用户邮箱",
        "user_account": "用户账号",
        "bitrate": "码率",
        "resolution": "分辨率",
        "fps": "帧率",
        "export_path": "导出路径",
    }
    items = []
    for key, value in meta.items():
        if value is None or value == "":
            continue
        items.append({
            "label": label_map.get(str(key), str(key).replace("_", " ").title()),
            "value": _format_meta_value(str(key), value),
        })
    return items


def _build_display(summary: str, detail: str, meta: dict):
    summary_text = str(summary or "").strip()
    detail_text = str(detail or "").strip()
    detail_map = {
        "image_export": "图片导出",
        "video_export": "视频导出",
        "frontend_export": "前端导出",
        "apply_profile": "配置应用",
        "apply_style": "风格应用",
        "neural_preset": "NeuralPreset",
        "modflows_b0": "ModFlows B0",
        "modflows_b6": "ModFlows B6",
    }
    if detail_text in detail_map:
        detail_text = detail_map[detail_text]
    if not detail_text:
        detail_text = detail_map.get(str(meta.get("source") or "").strip(), summary_text)
    return {
        "summary": summary_text,
        "detail": detail_text,
    }


def _load_runtime_stats():
    if not RUNTIME_STATS_PATH.exists():
        raise FileNotFoundError(f"未找到运行时日志文件: {RUNTIME_STATS_PATH}")
    with open(RUNTIME_STATS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_runtime_stats(payload):
    with open(RUNTIME_STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _user_lookup_keys(user: User):
    keys = set()
    if user is None:
        return keys
    keys.add(str(user.id).strip().lower())
    for value in (user.email, user.phone, user.qq_id, user.wechat_id):
        raw = str(value or "").strip().lower()
        if raw:
            keys.add(raw)
    display_name = str(_derive_display_name(user) or "").strip().lower()
    if display_name:
        keys.add(display_name)
    keys.add(f"user_{user.id}_user")
    keys.add(f"user_{user.id}_admin")
    return keys


def _collect_candidate_keys(entry: dict):
    keys = []

    def add(value):
        raw = str(value or "").strip().lower()
        if not raw or raw in keys:
            return
        keys.append(raw)
        match = TASK_LOG_USER_KEY_PATTERN.fullmatch(raw)
        if match:
            user_id = match.group(1)
            if user_id and user_id not in keys:
                keys.append(user_id)

    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    add(entry.get("user_id"))
    add(entry.get("email"))
    add(entry.get("user_label"))
    add(meta.get("email"))
    add(meta.get("user_email"))
    add(meta.get("user_account"))
    return keys


async def _load_user_map():
    async with async_session() as session:
        users = (await session.execute(select(User))).scalars().all()
    user_map = {}
    for user in users:
        for key in _user_lookup_keys(user):
            user_map[key] = user
    return user_map


async def backfill_admin_task_logs():
    payload = _load_runtime_stats()
    logs = payload.get("task_logs")
    if not isinstance(logs, list):
        raise ValueError("admin_runtime_metrics.json 中 task_logs 格式不正确")

    user_map = await _load_user_map()
    updated = 0

    for entry in logs:
        if not isinstance(entry, dict):
            continue
        matched_user = None
        for key in _collect_candidate_keys(entry):
            matched_user = user_map.get(key)
            if matched_user:
                break
        changed = False
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        normalized_user_id = entry.get("user_id")
        normalized_label = str(entry.get("user_label") or meta.get("user_account") or "游客").strip() or "游客"
        normalized_email = str(entry.get("email") or meta.get("user_email") or meta.get("email") or "").strip()
        normalized_role = str(entry.get("role") or "user").strip() or "user"
        avatar_path = str(entry.get("avatar_url") or "").strip()

        if matched_user:
            display_name = _derive_display_name(matched_user)
            profile_record = _user_profile_record(matched_user.id)
            normalized_user_id = matched_user.id
            normalized_label = display_name or matched_user.email or entry.get("user_label") or f"user_{matched_user.id}"
            normalized_email = matched_user.email or normalized_email
            normalized_role = matched_user.role or normalized_role
            if isinstance(profile_record, dict):
                avatar_path = str(profile_record.get("avatar_path") or avatar_path).strip()

        if entry.get("user_id") != normalized_user_id:
            entry["user_id"] = normalized_user_id
            changed = True
        if entry.get("user_label") != normalized_label:
            entry["user_label"] = normalized_label
            changed = True
        if entry.get("email") != normalized_email:
            entry["email"] = normalized_email
            changed = True
        if entry.get("role") != normalized_role:
            entry["role"] = normalized_role
            changed = True
        if avatar_path and entry.get("avatar_url") != avatar_path:
            entry["avatar_url"] = avatar_path
            changed = True

        if normalized_email and meta.get("user_email") != normalized_email:
            meta["user_email"] = normalized_email
            changed = True
        if normalized_label and meta.get("user_account") != normalized_label:
            meta["user_account"] = normalized_label
            changed = True
        entry["meta"] = meta
        entry["meta_raw"] = dict(meta)

        user_payload = {
            "id": normalized_user_id if isinstance(normalized_user_id, int) else None,
            "display_name": normalized_label,
            "email": normalized_email,
            "role": normalized_role,
            "avatar_url": avatar_path,
        }
        if entry.get("user") != user_payload:
            entry["user"] = user_payload
            changed = True

        display_payload = _build_display(entry.get("summary"), entry.get("detail"), meta)
        if entry.get("display") != display_payload:
            entry["display"] = display_payload
            changed = True
        if entry.get("summary") != display_payload["summary"]:
            entry["summary"] = display_payload["summary"]
            changed = True
        if entry.get("detail") != display_payload["detail"]:
            entry["detail"] = display_payload["detail"]
            changed = True

        timing_payload = {"duration_ms": entry.get("duration_ms")}
        if entry.get("timing") != timing_payload:
            entry["timing"] = timing_payload
            changed = True

        meta_display = _build_meta_display(meta)
        if entry.get("meta_display") != meta_display:
            entry["meta_display"] = meta_display
            changed = True

        if int(entry.get("schema_version") or 0) < 2:
            entry["schema_version"] = 2
            changed = True

        if changed:
            updated += 1

    if updated:
        _save_runtime_stats(payload)
    return {
        "log_file": str(RUNTIME_STATS_PATH),
        "updated_logs": updated,
        "total_logs": len(logs),
    }


async def main():
    result = await backfill_admin_task_logs()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
