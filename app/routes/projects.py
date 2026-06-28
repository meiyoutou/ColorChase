import os
import uuid
import shutil
import json
import re
from pathlib import Path
from collections import Counter
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, desc, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone

from admin_runtime_metrics import get_monthly_user_usage, load_runtime_stats, record_export, record_task_log, record_user_usage
from database import async_session, get_db
from models import User, Project, Asset
from app.routes.auth import get_current_user
from app.security import ensure_upload_file_size
from config import (
    BASE_DIR,
    get_project_assets_dir,
    get_training_corpus_dir,
    get_user_assets_dir,
    get_user_images_dir,
    get_user_profiles_dir,
    get_user_references_dir,
    iter_known_project_asset_dirs,
)

router = APIRouter()

USER_LOCAL_ROOT = get_user_assets_dir() / "local_user"
USER_LOCAL_IMAGE_DIR = get_user_images_dir()
USER_LOCAL_REFERENCE_DIR = get_user_references_dir()
USER_LOCAL_PROFILE_DIR = get_user_profiles_dir()
USER_TRAINING_DIR = get_training_corpus_dir()
USER_SPACE_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
USER_SPACE_AUTO_REFRESH_SECONDS = 60
USER_SPACE_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
USER_SPACE_CORE_TASK_TYPES = {"图片追色", "视频追色", "模型训练"}
USER_SPACE_MODEL_LABELS = {
    "reinhard": "经典追色",
    "histogram": "直方图追色",
    "luminance_partition": "亮度分区追色",
    "neural_preset": "NeuralPreset",
    "modflows": "ModFlows",
    "regional_modflows": "区域 ModFlows",
    "regional_luminance": "区域亮度追色",
    "ai_portrait": "AI 人像追色",
    "dncm_lut": "NeuralPreset LUT",
    "modflows_b0": "ModFlows B0",
    "modflows_b6": "ModFlows B6",
}
SIZE_MODE_LABELS = {
    "full": "原尺寸",
    "original": "原尺寸",
    "half": "50% 画质",
    "2x": "2x 超分",
}
USER_PROFILE_META_PATH = USER_LOCAL_PROFILE_DIR / "user_profiles.json"
USER_PROFILE_AVATAR_DIR = USER_LOCAL_PROFILE_DIR / "avatars"
USER_PROFILE_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
USER_PROFILE_MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _project_assets_root() -> Path:
    return get_project_assets_dir()


def _project_asset_roots():
    roots = []
    seen = set()
    for root in list(iter_known_project_asset_dirs()) + [_project_assets_root()]:
        try:
            resolved = root.resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _project_storage_roots(project_id: int):
    roots = []
    seen = set()
    for root in _project_asset_roots():
        candidate = root / str(project_id)
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _safe_project_asset_url(project_id: int, *parts: str) -> str:
    safe_parts = [Path(str(part or "")).name for part in parts if str(part or "").strip()]
    return "/api/project_assets/" + str(int(project_id)) + "/" + "/".join(safe_parts)


def _read_user_profile_store():
    if not USER_PROFILE_META_PATH.exists():
        return {}
    try:
        payload = json.loads(USER_PROFILE_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_user_profile_store(payload):
    USER_PROFILE_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_PROFILE_META_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _user_profile_record(user_id: int):
    store = _read_user_profile_store()
    record = store.get(str(user_id))
    return record if isinstance(record, dict) else {}


def normalize_legacy_user_asset_url(value):
    raw = str(value or "").strip()
    if not raw:
        return raw
    if raw.startswith("/assets/local_user/"):
        return "/api/user_assets/" + raw[len("/assets/local_user/"):].lstrip("/")
    return raw


def _user_avatar_url(record: dict) -> str:
    avatar_path = str(record.get("avatar_path") or "").strip()
    if not avatar_path:
        return ""
    resolved = _resolve_existing_path(avatar_path)
    if not resolved or not resolved.exists():
        return ""
    updated_at = str(record.get("updated_at") or "").strip()
    cache_key = updated_at.replace(":", "").replace("-", "").replace("T", "").replace("Z", "")
    avatar_path = normalize_legacy_user_asset_url(avatar_path)
    return avatar_path + (f"?v={cache_key}" if cache_key else "")


def migrate_legacy_user_asset_references() -> dict:
    updated_profiles = 0
    updated_project_snapshots = 0

    store = _read_user_profile_store()
    changed_store = False
    for key, record in list(store.items()):
        if not isinstance(record, dict):
            continue
        avatar_path = str(record.get("avatar_path") or "").strip()
        normalized = normalize_legacy_user_asset_url(avatar_path)
        if normalized != avatar_path:
            record["avatar_path"] = normalized
            store[key] = record
            updated_profiles += 1
            changed_store = True
    if changed_store:
        _write_user_profile_store(store)

    return {
        "updated_profiles": updated_profiles,
        "updated_project_snapshots": updated_project_snapshots,
    }


def _rewrite_legacy_asset_urls(obj):
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            obj[key] = _rewrite_legacy_asset_urls(value)
        return obj
    if isinstance(obj, list):
        return [_rewrite_legacy_asset_urls(item) for item in obj]
    if isinstance(obj, str):
        return normalize_legacy_user_asset_url(obj)
    return obj


async def run_startup_legacy_asset_migration() -> dict:
    stats = migrate_legacy_user_asset_references()

    async with async_session() as db:
        result = await db.execute(select(Project))
        projects = result.scalars().all()
        changed = 0
        for project in projects:
            raw_snapshot = str(project.workspace_snapshot or "").strip()
            if not raw_snapshot:
                continue
            try:
                parsed = json.loads(raw_snapshot)
            except Exception:
                continue
            rewritten = _rewrite_legacy_asset_urls(parsed)
            normalized = json.dumps(rewritten, ensure_ascii=False)
            if normalized != raw_snapshot:
                project.workspace_snapshot = normalized
                changed += 1
        if changed:
            await db.commit()
        stats["updated_project_snapshots"] = changed

    return {
        "updated_profiles": stats["updated_profiles"],
        "updated_project_snapshots": stats["updated_project_snapshots"],
    }


def _mb(size_bytes: int) -> float:
    return round(float(size_bytes or 0) / 1024 / 1024, 2)


def _safe_file_size(path):
    try:
        return path.stat().st_size if path and path.exists() and path.is_file() else 0
    except OSError:
        return 0


def _looks_like_thumb(name: str) -> bool:
    lower = str(name or "").lower()
    return lower.endswith(("_thumb.jpg", "_thumb.jpeg", "_thumb.png"))


def _looks_like_export_name(name: str) -> bool:
    lower = str(name or "").lower()
    return bool(
        lower.startswith("export_")
        or "result" in lower
        or re.search(r"(^|[_-])ai(\.[a-z0-9]+)?$", lower)
    )


def _read_project_snapshot(raw_snapshot: str):
    if not raw_snapshot:
        return {}
    try:
        parsed = json.loads(raw_snapshot)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_existing_path(value):
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith(("data:", "blob:", "http://", "https://")):
        return None
    raw = raw.split("?", 1)[0]
    if raw.startswith("/api/project_assets/"):
        parts = raw[len("/api/project_assets/"):].split("/", 1)
        if len(parts) == 2 and parts[0].isdigit():
            relative = Path(parts[1].replace("\\", "/"))
            if not relative.is_absolute() and not any(part in ("", ".", "..") for part in relative.parts):
                for root in _project_asset_roots():
                    candidate = (root / parts[0] / relative).resolve()
                    project_root = (root / parts[0]).resolve()
                    if (candidate == project_root or project_root in candidate.parents) and candidate.exists():
                        return candidate
        return None
    if raw.startswith("/assets/projects/"):
        parts = raw[len("/assets/projects/"):].split("/", 1)
        if len(parts) == 2 and parts[0].isdigit():
            relative = Path(parts[1].replace("\\", "/"))
            if not relative.is_absolute() and not any(part in ("", ".", "..") for part in relative.parts):
                for root in _project_asset_roots():
                    candidate = (root / parts[0] / relative).resolve()
                    project_root = (root / parts[0]).resolve()
                    if (candidate == project_root or project_root in candidate.parents) and candidate.exists():
                        return candidate
    if raw.startswith("/api/user_assets/"):
        parts = raw[len("/api/user_assets/"):].split("/", 1)
        user_roots = {
            "images": USER_LOCAL_IMAGE_DIR,
            "references": USER_LOCAL_REFERENCE_DIR,
            "profiles": USER_LOCAL_PROFILE_DIR,
        }
        if len(parts) == 2 and parts[0] in user_roots:
            relative = Path(parts[1].replace("\\", "/"))
            if not relative.is_absolute() and not any(part in ("", ".", "..") for part in relative.parts):
                root = user_roots[parts[0]].resolve()
                candidate = (root / relative).resolve()
                if (candidate == root or root in candidate.parents) and candidate.exists():
                    return candidate
    if raw.startswith("/assets/"):
        candidate = get_user_assets_dir() / raw[len("/assets/"):]
    elif raw.startswith("/videos/") or raw.startswith("/styles/") or raw.startswith("/uploaded/"):
        candidate = BASE_DIR / raw.lstrip("/")
    else:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = BASE_DIR / raw.lstrip("/")
    return candidate if candidate.exists() else None


def _snapshot_has_custom_preset(snapshot: dict) -> bool:
    if not isinstance(snapshot, dict):
        return False
    profile_builtin = str(snapshot.get("profileBuiltin") or "").strip()
    lut_profile = snapshot.get("lutProfile") if isinstance(snapshot.get("lutProfile"), dict) else {}
    adjust_sliders = snapshot.get("adjustSliders") if isinstance(snapshot.get("adjustSliders"), dict) else {}
    if profile_builtin and profile_builtin != "standard":
        return True
    if lut_profile:
        return True
    for value in adjust_sliders.values():
        try:
            if float(value or 0) != 100.0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _display_model_label(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    return USER_SPACE_MODEL_LABELS.get(normalized, normalized)


def _display_size_mode(size_mode: str) -> str:
    normalized = str(size_mode or "").strip().lower()
    return SIZE_MODE_LABELS.get(normalized, str(size_mode or "").strip())


def _snapshot_reference_label(snapshot: dict) -> str:
    if not isinstance(snapshot, dict):
        return ""
    if snapshot.get("refDataUrl") or snapshot.get("refSavedPath"):
        return "参考图驱动"
    lut_profile = snapshot.get("lutProfile")
    if isinstance(lut_profile, dict) and str(lut_profile.get("type") or "").lower() == "custom":
        return "自定义 LUT"
    profile_builtin = str(snapshot.get("profileBuiltin") or "").strip()
    if profile_builtin:
        return _display_model_label(profile_builtin)
    return ""


def _snapshot_model_label(snapshot: dict) -> str:
    if not isinstance(snapshot, dict):
        return ""
    algorithm = str(snapshot.get("algorithm") or "").strip()
    if algorithm:
        return _display_model_label(algorithm)
    profile_builtin = str(snapshot.get("profileBuiltin") or "").strip()
    if profile_builtin:
        return _display_model_label(profile_builtin)
    lut_profile = snapshot.get("lutProfile")
    if isinstance(lut_profile, dict) and str(lut_profile.get("type") or "").lower() == "custom":
        return "自定义模型"
    return ""


def _build_user_project_inventory(projects, asset_rows):
    asset_map = {}
    for asset in asset_rows:
        asset_map.setdefault(asset.project_id, []).append(asset)

    project_storage_files = set()
    original_size_files = set()
    reference_size_files = set()
    export_size_files = set()
    project_asset_count = 0
    original_count = 0
    original_size = 0
    reference_count = 0
    reference_size = 0
    export_count = 0
    export_size = 0
    custom_preset_count = 0
    reference_style_counter = Counter()
    snapshot_model_counter = Counter()
    latest_snapshot_model = ""

    for project in projects:
        project_assets = asset_map.get(project.id, [])
        project_asset_count += len(project_assets)
        snapshot = _read_project_snapshot(project.workspace_snapshot or "")
        snapshot_targets = snapshot.get("targetImages") if isinstance(snapshot.get("targetImages"), list) else []
        project_original_count = 0
        project_reference_count = 0
        project_export_count = 0
        dir_original_count = 0
        dir_reference_count = 0
        dir_export_count = 0

        if snapshot_targets:
            project_original_count = len(snapshot_targets)
            for item in snapshot_targets:
                if not isinstance(item, dict):
                    continue
                resolved = _resolve_existing_path(item.get("savedPath") or item.get("sourcePath"))
                if resolved and resolved.is_file():
                    project_storage_files.add(resolved)
                    if resolved not in original_size_files:
                        original_size_files.add(resolved)
                        original_size += _safe_file_size(resolved)
        else:
            project_original_count = sum(
                1 for asset in project_assets if not _looks_like_export_name(asset.file_name)
            )

        ref_path = _resolve_existing_path(snapshot.get("refSavedPath"))
        if ref_path and ref_path.is_file():
            project_storage_files.add(ref_path)
            project_reference_count = 1
            if ref_path not in reference_size_files:
                reference_size_files.add(ref_path)
                reference_size += _safe_file_size(ref_path)
        elif snapshot.get("refDataUrl"):
            project_reference_count = 1

        video_source = _resolve_existing_path(snapshot.get("videoFileSavedPath"))
        if video_source and video_source.is_file():
            project_storage_files.add(video_source)

        video_result = _resolve_existing_path(snapshot.get("videoResultUrl"))
        if video_result and video_result.is_file():
            project_storage_files.add(video_result)
            project_export_count = 1
            if video_result not in export_size_files:
                export_size_files.add(video_result)
                export_size += _safe_file_size(video_result)

        for root in _project_storage_roots(project.id):
            if not root.exists():
                continue
            for file_path in root.rglob("*"):
                if not file_path.is_file():
                    continue
                project_storage_files.add(file_path)
                if _looks_like_thumb(file_path.name):
                    continue
                lower_parts = {part.lower() for part in file_path.parts}
                if "reference" in lower_parts or file_path.parent.name.lower() == "reference":
                    dir_reference_count += 1
                    if file_path not in reference_size_files:
                        reference_size_files.add(file_path)
                        reference_size += _safe_file_size(file_path)
                elif "originals" in lower_parts:
                    dir_original_count += 1
                    if file_path not in original_size_files:
                        original_size_files.add(file_path)
                        original_size += _safe_file_size(file_path)
                elif _looks_like_export_name(file_path.name) or file_path.parent.name.lower() in {"exports", "results", "export"}:
                    dir_export_count += 1
                    if file_path not in export_size_files:
                        export_size_files.add(file_path)
                        export_size += _safe_file_size(file_path)

        project_original_count = max(project_original_count, dir_original_count)
        project_reference_count = max(project_reference_count, dir_reference_count)
        project_export_count = max(project_export_count, dir_export_count)

        if project_export_count == 0:
            project_export_count = sum(1 for asset in project_assets if _looks_like_export_name(asset.file_name))

        original_count += project_original_count
        reference_count += project_reference_count
        export_count += project_export_count

        if _snapshot_has_custom_preset(snapshot):
            custom_preset_count += 1
        snapshot_model = _snapshot_model_label(snapshot)
        if snapshot_model:
            snapshot_model_counter[snapshot_model] += 1
            if not latest_snapshot_model:
                latest_snapshot_model = snapshot_model
        reference_label = _snapshot_reference_label(snapshot)
        if reference_label:
            reference_style_counter[reference_label] += 1

    return {
        "original_count": original_count,
        "original_size": original_size,
        "reference_count": reference_count,
        "reference_size": reference_size,
        "export_count": export_count,
        "export_size": export_size,
        "project_asset_count": project_asset_count,
        "project_storage_size": sum(_safe_file_size(path) for path in project_storage_files),
        "custom_preset_count": custom_preset_count,
        "reference_style": reference_style_counter.most_common(1)[0][0] if reference_style_counter else "",
        "latest_snapshot_model": latest_snapshot_model,
        "snapshot_model": snapshot_model_counter.most_common(1)[0][0] if snapshot_model_counter else "",
    }


def _parse_runtime_dt(value):
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(USER_SPACE_TZ)


def _format_runtime_dt(value) -> str:
    parsed = _parse_runtime_dt(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _scan_project_storage(project_ids):
    total_size = 0
    for project_id in project_ids:
        for project_dir in _project_storage_roots(project_id):
            if not project_dir.exists():
                continue
            for item in project_dir.rglob("*"):
                if not item.is_file():
                    continue
                try:
                    total_size += item.stat().st_size
                except OSError:
                    continue
    return total_size


def _remove_project_storage(project_id: int):
    for project_dir in _project_storage_roots(project_id):
        if not project_dir.exists():
            continue
        try:
            shutil.rmtree(project_dir)
        except OSError:
            continue


async def _purge_project_resources(db: AsyncSession, project_id: int):
    await db.execute(delete(Asset).where(Asset.project_id == project_id))
    _remove_project_storage(project_id)


def _user_runtime_logs(runtime_stats, user_id):
    task_logs = runtime_stats.get("task_logs", [])
    if not isinstance(task_logs, list):
        return []
    normalized_user_id = str(user_id)
    return [entry for entry in task_logs if str(entry.get("user_id")) == normalized_user_id]


def _is_core_result(entry):
    return (
        str(entry.get("task_type") or "") in USER_SPACE_CORE_TASK_TYPES
        and str(entry.get("event_type") or "").lower() == "result"
    )


def _is_export_result(entry):
    return (
        str(entry.get("task_type") or "") == "导出"
        and str(entry.get("event_type") or "").lower() == "result"
        and str(entry.get("status") or "").lower() == "ok"
    )


def _sum_logged_export_storage_bytes(logs):
    total = 0
    for entry in logs:
        if not _is_export_result(entry):
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        try:
            total += int(meta.get("export_size_bytes") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _active_task_count_from_logs(logs):
    started = {}
    finished = set()
    for entry in logs:
        task_id = str(entry.get("task_id") or "").strip()
        if not task_id:
            continue
        task_type = str(entry.get("task_type") or "")
        if task_type not in USER_SPACE_CORE_TASK_TYPES:
            continue
        event_type = str(entry.get("event_type") or "").lower()
        status = str(entry.get("status") or "").lower()
        if event_type == "request":
            started[task_id] = entry
        if event_type in {"result", "control"} and status in {"ok", "fail", "cancel"}:
            finished.add(task_id)
    return sum(1 for task_id in started if task_id not in finished)


def _build_user_daily_series(logs, usage_count_7d):
    today = datetime.now(USER_SPACE_TZ).date()
    daily_map = {}
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_key = day.strftime("%Y-%m-%d")
        daily_map[day_key] = {
            "date": day_key,
            "label": USER_SPACE_WEEKDAY_LABELS[day.weekday()],
            "tasks": 0,
            "exports": 0,
            "failed": 0,
            "usage": int(usage_count_7d.get(day_key, 0)),
        }

    for entry in logs:
        parsed = _parse_runtime_dt(entry.get("created_at"))
        if parsed is None:
            continue
        day_key = parsed.strftime("%Y-%m-%d")
        if day_key not in daily_map:
            continue
        status = str(entry.get("status") or "").lower()
        if _is_core_result(entry) and status in {"ok", "fail", "cancel"}:
            daily_map[day_key]["tasks"] += 1
            if status == "fail":
                daily_map[day_key]["failed"] += 1
        if _is_export_result(entry):
            daily_map[day_key]["exports"] += 1

    return [daily_map[key] for key in sorted(daily_map.keys())]


def _build_active_task_series(logs):
    today = datetime.now(USER_SPACE_TZ).date()
    daily = []
    events = []
    for entry in logs:
        parsed = _parse_runtime_dt(entry.get("created_at"))
        if parsed is None:
            continue
        task_type = str(entry.get("task_type") or "")
        if task_type not in USER_SPACE_CORE_TASK_TYPES:
            continue
        task_id = str(entry.get("task_id") or "").strip()
        event_type = str(entry.get("event_type") or "").lower()
        status = str(entry.get("status") or "").lower()
        if not task_id:
            continue
        if event_type == "request":
            events.append((parsed, task_id, 1))
        elif event_type in {"result", "control"} and status in {"ok", "fail", "cancel"}:
            events.append((parsed, task_id, -1))

    events.sort(key=lambda item: item[0])
    current = 0
    index = 0
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_end = datetime.combine(day, datetime.max.time(), tzinfo=USER_SPACE_TZ)
        while index < len(events) and events[index][0] <= day_end:
            current = max(0, current + events[index][2])
            index += 1
        daily.append(current)
    return daily


def _count_unique_exported_photos(projects, asset_rows):
    asset_map = {}
    for asset in asset_rows:
        asset_map.setdefault(asset.project_id, []).append(asset)

    exported_sources = set()

    for project in projects:
        snapshot = _read_project_snapshot(project.workspace_snapshot or "")
        snapshot_targets = snapshot.get("targetImages") if isinstance(snapshot.get("targetImages"), list) else []
        project_assets = asset_map.get(project.id, [])
        has_export = False

        video_result = _resolve_existing_path(snapshot.get("videoResultUrl"))
        if video_result and video_result.is_file():
            has_export = True

        if not has_export:
            has_export = any(_looks_like_export_name(asset.file_name) for asset in project_assets)

        if not has_export:
            for root in _project_storage_roots(project.id):
                if not root.exists():
                    continue
                for file_path in root.rglob("*"):
                    if not file_path.is_file() or _looks_like_thumb(file_path.name):
                        continue
                    parent_name = file_path.parent.name.lower()
                    if _looks_like_export_name(file_path.name) or parent_name in {"exports", "results", "export"}:
                        has_export = True
                        break
                if has_export:
                    break

        if not has_export:
            continue

        if snapshot_targets:
            for item in snapshot_targets:
                if not isinstance(item, dict):
                    continue
                raw_source = item.get("savedPath") or item.get("sourcePath") or item.get("name") or ""
                resolved = _resolve_existing_path(raw_source)
                identifier = str(resolved.resolve()) if resolved and resolved.exists() else str(raw_source).strip()
                if identifier:
                    exported_sources.add(identifier)
        elif (project.type or "").lower() != "video":
            exported_sources.add(f"project:{project.id}")

    return len(exported_sources)


def _count_unique_exported_photos_from_logs(logs):
    exported_sources = set()
    for entry in logs:
        if not _is_export_result(entry):
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        source_key = str(meta.get("source_image_key") or meta.get("source_path") or meta.get("file_name") or "").strip()
        if source_key:
            exported_sources.add(source_key)
            continue
        count = max(int(meta.get("export_file_count") or 0), 0)
        if count > 0:
            for idx in range(count):
                exported_sources.add(f"{entry.get('id')}#{idx}")
    return len(exported_sources)


def _build_project_series(projects):
    today = datetime.now(USER_SPACE_TZ).date()
    points = []
    for project in projects:
        if project.created_at is None:
            continue
        created = project.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created = created.astimezone(USER_SPACE_TZ)
        points.append(created.date())

    series = []
    running = 0
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        running = sum(1 for created_day in points if created_day <= day)
        series.append(running)
    return series


def _recent_user_logs(logs, limit=4):
    filtered = []
    for entry in reversed(logs):
        task_type = str(entry.get("task_type") or "")
        status = str(entry.get("status") or "").lower()
        if task_type == "任务控制":
            continue
        if status not in {"ok", "fail", "cancel", "info"}:
            continue
        filtered.append(
            {
                "created_at": _format_runtime_dt(entry.get("created_at")),
                "task_type": task_type or "未知任务",
                "status": status or "info",
                "summary": str(entry.get("summary") or "暂无摘要"),
                "model": str(entry.get("model") or ""),
            }
        )
        if len(filtered) >= max(int(limit or 0), 1):
            break
    return filtered


def _health_score(task_completion_rate, task_success_rate, activity_rate):
    score = (
        float(task_completion_rate or 0) * 0.35
        + float(task_success_rate or 0) * 0.35
        + float(activity_rate or 0) * 0.30
    )
    return round(score, 1)


def _count_files(root, allowed_suffixes=None, skip_suffixes=None):
    path = root
    if not path.exists():
        return 0, 0

    allowed = {item.lower() for item in (allowed_suffixes or [])}
    skipped = tuple(item.lower() for item in (skip_suffixes or []))
    total_files = 0
    total_size = 0
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        lower_name = file_path.name.lower()
        if skipped and lower_name.endswith(skipped):
            continue
        if allowed and file_path.suffix.lower() not in allowed:
            continue
        total_files += 1
        try:
            total_size += file_path.stat().st_size
        except OSError:
            continue
    return total_files, total_size


def _derive_display_name(user: User) -> str:
    profile_record = _user_profile_record(user.id)
    nickname = str(profile_record.get("nickname") or "").strip()
    if nickname:
        return nickname
    if user.email:
        return str(user.email).split("@", 1)[0]
    if user.phone:
        phone = str(user.phone)
        return phone[-4:] if len(phone) >= 4 else phone
    return f"用户{user.id}"


def _account_type_label(role: str) -> str:
    return "管理员账号" if str(role or "").lower() == "admin" else "普通用户"


def _status_label(status: str) -> str:
    mapping = {
        "ok": "成功",
        "fail": "失败",
        "info": "处理中",
        "cancel": "已取消",
    }
    return mapping.get(str(status or "").lower(), "未知")


def _action_schema(action_type: str, label: str):
    return {"type": action_type, "label": label}


def _recent_export_records(logs, limit=5):
    items = []
    for entry in reversed(logs):
        if not _is_export_result(entry):
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        export_format = str(meta.get("export_format") or "").strip()
        if not export_format and "视频导出" in str(entry.get("summary") or ""):
            export_format = str(entry.get("model") or "").strip().upper()
        items.append(
            {
                "created_at": _format_runtime_dt(entry.get("created_at")),
                "summary": str(entry.get("summary") or "导出完成"),
                "detail": str(entry.get("detail") or ""),
                "format": export_format or str(entry.get("model") or ""),
                "status": str(entry.get("status") or "ok").lower(),
            }
        )
        if len(items) >= max(int(limit or 0), 1):
            break
    return items


def _derive_export_preferences(logs):
    export_format = ""
    size_quality = ""
    for entry in reversed(logs):
        if not _is_export_result(entry):
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        summary = str(entry.get("summary") or "")
        detail = str(entry.get("detail") or "")
        model = str(entry.get("model") or "").strip()
        if not export_format:
            export_format = str(meta.get("export_format") or "").strip()
            if not export_format and "视频导出" in summary:
                export_format = model.upper()
        if not size_quality:
            size_quality = _display_size_mode(str(meta.get("size_mode") or "").strip())
            if not size_quality:
                resolution = str(meta.get("resolution") or "").strip()
                fps = str(meta.get("fps") or "").strip()
                if resolution or fps:
                    bits = []
                    if resolution:
                        bits.append(resolution)
                    if fps:
                        bits.append(f"{fps}fps" if fps != "original" else "原帧率")
                    size_quality = " / ".join(bits)
            if not size_quality and detail:
                size_quality = _display_size_mode(detail)
        if export_format and size_quality:
            break
    return export_format or "暂无导出记录", size_quality or "暂无导出记录"


def _recent_model_name(logs):
    for entry in reversed(logs):
        task_type = str(entry.get("task_type") or "")
        model_name = str(entry.get("model") or "").strip()
        if task_type not in USER_SPACE_CORE_TASK_TYPES or not model_name:
            continue
        return _display_model_label(model_name)
    return ""


def _recent_model_activity(logs, limit=5):
    items = []
    for entry in reversed(logs):
        task_type = str(entry.get("task_type") or "")
        status = str(entry.get("status") or "").lower()
        model_name = str(entry.get("model") or "").strip()
        if task_type not in USER_SPACE_CORE_TASK_TYPES:
            continue
        if status not in {"ok", "fail", "info", "cancel"}:
            continue
        if not model_name:
            continue
        items.append(
            {
                "created_at": _format_runtime_dt(entry.get("created_at")),
                "task_type": task_type or "未知任务",
                "status": status,
                "status_label": _status_label(status),
                "model": _display_model_label(model_name),
                "summary": str(entry.get("summary") or ""),
            }
        )
        if len(items) >= max(int(limit or 0), 1):
            break
    return items


def _model_share(logs):
    counts = Counter()
    for entry in logs:
        task_type = str(entry.get("task_type") or "")
        status = str(entry.get("status") or "").lower()
        model_name = str(entry.get("model") or "").strip()
        if task_type not in USER_SPACE_CORE_TASK_TYPES:
            continue
        if status not in {"ok", "fail", "info", "cancel"}:
            continue
        if not model_name:
            continue
        counts[_display_model_label(model_name)] += 1

    total = sum(counts.values())
    if total <= 0:
        return []

    palette = ["#5166f5", "#20bfa9", "#ff7d59", "#8a67f4", "#1fa4e6"]
    items = []
    for index, (label, value) in enumerate(counts.most_common(5)):
        items.append(
            {
                "label": label,
                "value": value,
                "percent": round(value / total * 100, 1),
                "color": palette[index % len(palette)],
            }
        )
    return items


def _task_center_items(logs, limit=6):
    items = []
    for entry in reversed(logs):
        task_type = str(entry.get("task_type") or "")
        if task_type == "浠诲姟鎺у埗":
            continue
        status = str(entry.get("status") or "").lower()
        if status not in {"ok", "fail", "info", "cancel"}:
            continue

        primary_action = _action_schema("open_projects", "继续处理")
        if task_type == "模型训练":
            primary_action = _action_schema("open_train", "继续训练")
        elif task_type == "导出":
            primary_action = _action_schema("open_home", "重新导出")

        items.append(
            {
                "task_id": str(entry.get("task_id") or ""),
                "created_at": _format_runtime_dt(entry.get("created_at")),
                "task_type": task_type or "未知任务",
                "status": status,
                "status_label": _status_label(status),
                "summary": str(entry.get("summary") or "暂无任务摘要"),
                "detail": str(entry.get("detail") or ""),
                "failure_reason": str(entry.get("detail") or "") if status == "fail" else "",
                "model": str(entry.get("model") or ""),
                "primary_action": primary_action,
                "secondary_action": _action_schema("view_log", "查看日志"),
            }
        )
        if len(items) >= max(int(limit or 0), 1):
            break
    return items


class CreateProjectRequest(BaseModel):
    name: str
    type: str


class ProjectResponse(BaseModel):
    id: int
    name: str
    type: str
    owner_id: int
    created_at: str


@router.post("/")
async def create_project(
    req: CreateProjectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.type not in ("image", "video"):
        raise HTTPException(status_code=400, detail="项目类型必须是 image 或 video")

    project = Project(
        name=req.name or "未命名项目",
        type=req.type,
        owner_id=user.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    return {
        "id": project.id,
        "name": project.name,
        "type": project.type,
        "owner_id": project.owner_id,
        "created_at": str(project.created_at) if project.created_at else "",
    }


@router.get("/")
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id, Project.deleted_at == None)
        .order_by(desc(Project.created_at))
    )
    projects = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "type": p.type,
            "owner_id": p.owner_id,
            "created_at": str(p.created_at) if p.created_at else "",
            "snapshot": p.workspace_snapshot or "",
        }
        for p in projects
    ]


@router.get("/space_dashboard")
async def user_space_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    runtime_stats = load_runtime_stats()

    projects_result = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id, Project.deleted_at == None)
        .order_by(desc(Project.created_at))
    )
    projects = projects_result.scalars().all()
    project_ids = [project.id for project in projects]

    total_assets = 0
    if project_ids:
        total_assets = int(
            await db.scalar(
                select(func.count(Asset.id)).where(Asset.project_id.in_(project_ids))
            )
            or 0
        )

    user_logs = _user_runtime_logs(runtime_stats, user.id)
    usage_map = runtime_stats.get("user_usage", {})
    usage_daily = usage_map.get(str(user.id), {}) if isinstance(usage_map, dict) else {}
    if not isinstance(usage_daily, dict):
        usage_daily = {}

    today = datetime.now(USER_SPACE_TZ).date()
    usage_count_7d = {}
    active_days = 0
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_key = day.strftime("%Y-%m-%d")
        count = int(usage_daily.get(day_key, 0) or 0)
        usage_count_7d[day_key] = count
        if count > 0:
            active_days += 1

    daily = _build_user_daily_series(user_logs, usage_count_7d)
    completed_tasks = sum(1 for entry in user_logs if _is_core_result(entry) and str(entry.get("status") or "").lower() == "ok")
    failed_tasks = sum(1 for entry in user_logs if _is_core_result(entry) and str(entry.get("status") or "").lower() == "fail")
    export_events = sum(1 for entry in user_logs if _is_export_result(entry))
    active_tasks = _active_task_count_from_logs(user_logs)
    task_total = completed_tasks + failed_tasks
    task_closure_base = completed_tasks + failed_tasks + active_tasks
    task_completion_rate = 0 if task_closure_base <= 0 else round(completed_tasks / max(task_closure_base, 1) * 100, 1)
    task_success_rate = 0 if task_total <= 0 else round(completed_tasks / max(task_total, 1) * 100, 1)
    task_failure_rate = 0 if task_total <= 0 else round(failed_tasks / max(task_total, 1) * 100, 1)
    task_export_rate = 0 if completed_tasks <= 0 else round(min(export_events, completed_tasks) / max(completed_tasks, 1) * 100, 1)

    monthly_usage = get_monthly_user_usage(runtime_stats)
    monthly_usage_count = int(monthly_usage.get(str(user.id), 0) or 0)
    project_storage_mb = _mb(_scan_project_storage(project_ids))
    project_count = len(project_ids)
    recent_projects = [
        {
            "id": project.id,
            "name": project.name or "未命名项目",
            "type": project.type or "image",
            "created_at": project.created_at.strftime("%Y-%m-%d %H:%M:%S") if project.created_at else "",
        }
        for project in projects[:4]
    ]
    recent_logs = _recent_user_logs(user_logs, limit=4)

    activity_rate = round(active_days / 7 * 100, 1)
    health_score = _health_score(task_completion_rate, task_success_rate, activity_rate)

    cards = [
        {
            "key": "completed_tasks",
            "label": "已完成任务",
            "value": completed_tasks,
            "unit": "个",
            "delta": None,
            "sparkline": [item["tasks"] for item in daily] + [completed_tasks],
        },
        {
            "key": "failed_tasks",
            "label": "任务失败数",
            "value": failed_tasks,
            "unit": "个",
            "delta": None,
            "sparkline": [item["failed"] for item in daily] + [failed_tasks],
        },
        {
            "key": "export_events",
            "label": "导出文件数",
            "value": export_events,
            "unit": "个",
            "delta": None,
            "sparkline": [item["exports"] for item in daily] + [export_events],
        },
        {
            "key": "project_count",
            "label": "我的项目数",
            "value": project_count,
            "unit": "个",
            "delta": None,
            "sparkline": [project_count, project_count, project_count, project_count, project_count],
        },
    ]

    rings = [
        {
            "label": "任务完成率",
            "value": completed_tasks,
            "percent": task_completion_rate,
            "delta": None,
            "color": "#3557f2",
        },
        {
            "label": "任务成功率",
            "value": completed_tasks,
            "percent": task_success_rate,
            "delta": None,
            "color": "#4866ff",
        },
        {
            "label": "近 7 日活跃度",
            "value": active_days,
            "percent": activity_rate,
            "delta": None,
            "color": "#ff4a4a",
        },
    ]

    bars = [
        {
            "label": item["label"],
            "tasks": item["tasks"],
            "exports": item["exports"],
        }
        for item in daily
    ]

    progress = [
        {
            "label": "近 7 日活跃占比",
            "value": activity_rate,
            "delta": None,
            "delta_unit": "%",
            "color": "#4658f5",
        },
        {
            "label": "任务导出率",
            "value": task_export_rate,
            "delta": None,
            "delta_unit": "%",
            "color": "#26b86f",
        },
        {
            "label": "任务失败率",
            "value": task_failure_rate,
            "delta": None,
            "delta_unit": "%",
            "color": "#8a66dd",
        },
        {
            "label": "月使用活跃度",
            "value": min(100, round(monthly_usage_count / 100 * 100, 1)),
            "delta": None,
            "delta_unit": "%",
            "color": "#ff8c66",
        },
    ]

    categories = [
        {
            "label": "项目资产",
            "value": f"{total_assets} 个",
            "delta": None,
            "delta_text": None,
            "color": "#4658f5",
        },
        {
            "label": "项目存储",
            "value": f"{project_storage_mb:.1f} MB",
            "delta": None,
            "delta_text": None,
            "color": "#26b86f",
        },
        {
            "label": "最近 7 日活跃",
            "value": f"{active_days} / 7 天",
            "delta": None,
            "delta_text": None,
            "color": "#f05f70",
        },
        {
            "label": "月使用次数",
            "value": f"{monthly_usage_count} 次",
            "delta": None,
            "delta_text": None,
            "color": "#2aa9d6",
        },
        {
            "label": "失败任务数",
            "value": f"{failed_tasks} 个",
            "delta": None,
            "delta_text": None,
            "color": "#8a66dd",
        },
        {
            "label": "活跃任务数",
            "value": f"{active_tasks} 个",
            "delta": None,
            "delta_text": None,
            "color": "#3557f2",
        },
    ]

    logs = [
        f"近 7 日你活跃了 {active_days} / 7 天，累计完成 {completed_tasks} 个任务。",
        f"任务成功率 {task_success_rate:.1f}% ，失败率 {task_failure_rate:.1f}% ，导出率 {task_export_rate:.1f}%。",
        f"当前共有 {project_count} 个项目、{total_assets} 个资产，本月已使用 {monthly_usage_count} 次。",
    ]

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "meta": {
            "auto_refresh_seconds": USER_SPACE_AUTO_REFRESH_SECONDS,
            "compare_label": "较上周",
            "compare_empty_label": "当前周期",
            "weekly_refresh_rule": "个人空间自动更新",
            "health_score": health_score,
            "health_score_label": "个人健康度",
            "health_score_caption": "个人使用状态预估",
            "brand_mark": "User Space",
            "top_title": "My Space",
            "top_subtitle": "个人创作与任务状态总览",
            "hero_title": "个人核心数据与使用趋势",
            "hero_subtitle": "围绕你自己的项目、任务、导出与资产",
            "ring_title": "个人运行状态",
            "ring_subtitle": "任务完成率、任务成功率、近 7 日活跃度",
            "progress_title": "个人辅助指标",
            "progress_subtitle": "与你自己的使用行为相关",
            "category_title": "我的资源与资产",
            "category_subtitle": "仅统计当前账号内容",
            "log_title": "我的最近动态",
            "log_subtitle": "最近任务与项目摘要",
            "show_admin_logs": False,
        },
        "cards": cards,
        "rings": rings,
        "bars": bars,
        "progress": progress,
        "categories": categories,
        "logs": logs,
        "recent_logs": recent_logs,
        "recent_projects": recent_projects,
        "overview": {
            "user": {
                "id": user.id,
                "phone": user.phone,
                "email": user.email,
                "role": user.role,
                "created_at": user.created_at.strftime("%Y-%m-%d %H:%M:%S") if user.created_at else "",
            },
            "projects": {
                "total": project_count,
                "recent": recent_projects,
            },
            "assets": {
                "total": total_assets,
                "storage_mb": project_storage_mb,
            },
            "task_stats": {
                "completed_tasks": completed_tasks,
                "failed_tasks": failed_tasks,
                "active_tasks": active_tasks,
                "task_total": task_total,
                "task_completion_rate": task_completion_rate,
                "task_success_rate": task_success_rate,
                "task_failure_rate": task_failure_rate,
                "task_export_rate": task_export_rate,
                "export_events": export_events,
                "daily": daily,
                "recent_logs": recent_logs,
            },
            "usage": {
                "active_days_7d": active_days,
                "monthly_usage_count": monthly_usage_count,
            },
        },
    }


@router.get("/space_dashboard_v2")
async def user_space_dashboard_v2(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    runtime_stats = load_runtime_stats()

    projects_result = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id, Project.deleted_at == None)
        .order_by(desc(Project.created_at))
    )
    projects = projects_result.scalars().all()
    project_ids = [project.id for project in projects]

    assets_result = await db.execute(select(Asset).where(Asset.project_id.in_(project_ids))) if project_ids else None
    asset_rows = assets_result.scalars().all() if assets_result else []

    user_logs = _user_runtime_logs(runtime_stats, user.id)
    usage_map = runtime_stats.get("user_usage", {})
    usage_daily = usage_map.get(str(user.id), {}) if isinstance(usage_map, dict) else {}
    if not isinstance(usage_daily, dict):
        usage_daily = {}

    today = datetime.now(USER_SPACE_TZ).date()
    usage_count_7d = {}
    active_days = 0
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_key = day.strftime("%Y-%m-%d")
        count = int(usage_daily.get(day_key, 0) or 0)
        usage_count_7d[day_key] = count
        if count > 0:
            active_days += 1

    daily = _build_user_daily_series(user_logs, usage_count_7d)
    completed_tasks = sum(1 for entry in user_logs if _is_core_result(entry) and str(entry.get("status") or "").lower() == "ok")
    failed_tasks = sum(1 for entry in user_logs if _is_core_result(entry) and str(entry.get("status") or "").lower() == "fail")
    export_events = sum(1 for entry in user_logs if _is_export_result(entry))
    active_tasks = _active_task_count_from_logs(user_logs)
    task_total = completed_tasks + failed_tasks
    task_closure_base = completed_tasks + failed_tasks + active_tasks
    task_completion_rate = 0 if task_closure_base <= 0 else round(completed_tasks / max(task_closure_base, 1) * 100, 1)
    task_success_rate = 0 if task_total <= 0 else round(completed_tasks / max(task_total, 1) * 100, 1)
    task_failure_rate = 0 if task_total <= 0 else round(failed_tasks / max(task_total, 1) * 100, 1)
    task_export_rate = 0 if completed_tasks <= 0 else round(min(export_events, completed_tasks) / max(completed_tasks, 1) * 100, 1)
    activity_rate = round(active_days / 7 * 100, 1)
    health_score = _health_score(task_completion_rate, task_success_rate, activity_rate)

    monthly_usage = get_monthly_user_usage(runtime_stats)
    monthly_usage_count = int(monthly_usage.get(str(user.id), 0) or 0)
    inventory = _build_user_project_inventory(projects, asset_rows)
    operated_task_count = sum(
        1
        for entry in user_logs
        if _is_core_result(entry) and str(entry.get("status") or "").lower() in {"ok", "fail", "cancel"}
    )
    unique_exported_photo_count = max(
        _count_unique_exported_photos(projects, asset_rows),
        _count_unique_exported_photos_from_logs(user_logs),
    )
    export_storage_bytes = _sum_logged_export_storage_bytes(user_logs)
    if export_storage_bytes <= 0:
        export_storage_bytes = int(inventory["export_size"] or 0)
    else:
        export_storage_bytes = max(export_storage_bytes, int(inventory["export_size"] or 0))

    rated_asset_count = 0
    rateable_asset_count = 0
    for asset in asset_rows:
        file_name = str(getattr(asset, "file_name", "") or "").strip()
        if not file_name:
            continue
        rateable_asset_count += 1
        try:
            if int(getattr(asset, "rating", 0) or 0) > 0:
                rated_asset_count += 1
        except (TypeError, ValueError):
            continue

    display_name = _derive_display_name(user)
    model_share = _model_share(user_logs)
    common_model = model_share[0]["label"] if model_share else (inventory["snapshot_model"] or "暂无模型记录")
    recent_model = _recent_model_name(user_logs) or inventory["latest_snapshot_model"] or common_model
    export_format, size_quality = _derive_export_preferences(user_logs)
    active_task_series = _build_active_task_series(user_logs)
    project_series = _build_project_series(projects)
    task_efficiency = round((completed_tasks + export_events * 0.6) / max(active_days or 1, 1), 1)
    recent_projects = [
        {
            "id": project.id,
            "name": project.name or "未命名项目",
            "type": project.type or "image",
            "created_at": project.created_at.strftime("%Y-%m-%d %H:%M:%S") if project.created_at else "",
            "snapshot": bool(project.workspace_snapshot),
            "resume_action": {"type": "open_project", "project_id": project.id, "label": "一键回到上次编辑状态"},
        }
        for project in projects[:5]
    ]

    recent_tasks = _task_center_items(user_logs, limit=8)
    recent_exports = _recent_export_records(user_logs, limit=5)
    recent_model_records = _recent_model_activity(user_logs, limit=5)
    recent_logs = _recent_user_logs(user_logs, limit=4)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "meta": {
            "auto_refresh_seconds": USER_SPACE_AUTO_REFRESH_SECONDS,
            "weekly_refresh_rule": "个人空间自动更新",
            "health_score": health_score,
            "health_score_label": "个人健康度",
            "health_score_caption": "个人使用状态预估",
            "compare_label": "较上周",
            "compare_empty_label": "当前周期",
        },
        "profile": {
            "display_name": display_name,
            "avatar_text": display_name[:1].upper(),
            "avatar_url": _user_avatar_url(_user_profile_record(user.id)),
            "account_type": _account_type_label(user.role),
            "role": user.role,
            "account_id": user.email or user.phone or f"用户{user.id}",
            "created_at": user.created_at.strftime("%Y-%m-%d %H:%M:%S") if user.created_at else "",
            "last_login_at": user.last_active_at.strftime("%Y-%m-%d %H:%M:%S") if user.last_active_at else "",
            "rating_summary": {
                "rated_count": rated_asset_count,
                "total_count": rateable_asset_count,
            },
        },
        "cards": [
            {"key": "operated_tasks", "label": "操作任务", "value": operated_task_count, "unit": "个", "sparkline": [item["tasks"] for item in daily]},
            {"key": "export_events", "label": "导出次数", "value": export_events, "unit": "次", "sparkline": [item["exports"] for item in daily]},
            {"key": "export_events", "label": "导出文件数", "value": export_events, "unit": "个", "sparkline": [item["exports"] for item in daily]},
            {"key": "project_count", "label": "我的项目数", "value": len(projects), "unit": "个", "sparkline": project_series},
            {"key": "task_efficiency", "label": "我的创作效率", "value": task_efficiency, "unit": "", "sparkline": [item["tasks"] + item["exports"] for item in daily]},
        ],
        "bars": daily,
        "task_dashboard": {
            "operated_task_count": operated_task_count,
            "task_success_rate": task_success_rate,
            "task_export_rate": task_export_rate,
            "task_failure_rate": task_failure_rate,
            "model_share": model_share,
        },
        "resources": [
            {"label": "已上传原图数量", "value": f"{inventory['original_count']} 张", "size": f"{_mb(inventory['original_size']):.1f} MB"},
            {"label": "参考图数量", "value": f"{inventory['reference_count']} 张", "size": f"占用 {_mb(inventory['reference_size']):.1f} MB"},
            {"label": "导出占用空间", "value": f"{_mb(export_storage_bytes):.1f} MB", "size": f"{max(export_events, inventory['export_count'])} 个文件"},
            {"label": "项目资产数量", "value": f"{inventory['project_asset_count']} 个", "size": f"{_mb(inventory['project_storage_size']):.1f} MB"},
        ],
        "task_center": {
            "items": recent_tasks,
        },
        "preferences": {
            "common_model": common_model,
            "recent_model": recent_model,
            "default_export_format": export_format,
            "default_size_quality": size_quality,
            "preset_count": inventory["custom_preset_count"],
            "reference_style": inventory["reference_style"] or "暂无参考图",
            "model_share": model_share,
        },
        "history": {
            "recent_projects": recent_projects,
            "recent_exports": recent_exports,
            "recent_model_records": recent_model_records,
            "resume_entry": recent_projects[0]["resume_action"] if recent_projects else None,
        },
        "account": {
            "settings_entries": [
                {"type": "security", "label": "账户与安全", "description": "修改密码、绑定邮箱/手机号"},
                {"type": "storage", "label": "存储路径与导出设置", "description": "管理项目地址与默认导出参数"},
                {"type": "notifications", "label": "通知偏好", "description": "任务完成、导出完成和失败提醒"},
            ],
            "login_device": "当前浏览器会话",
            "last_login_at": user.last_active_at.strftime("%Y-%m-%d %H:%M:%S") if user.last_active_at else "",
        },
        "logs": [
            f"近 7 日你活跃了 {active_days} / 7 天，累计操作 {operated_task_count} 个任务。",
            f"任务成功率 {task_success_rate:.1f}% ，失败率 {task_failure_rate:.1f}% ，导出率 {task_export_rate:.1f}%。",
            f"当前共有 {len(projects)} 个项目、{inventory['project_asset_count']} 个资产项，累计导出 {export_events} 次。",
        ],
        "recent_logs": recent_logs,
        "overview": {
            "user": {
                "id": user.id,
                "phone": user.phone,
                "email": user.email,
                "role": user.role,
                "created_at": user.created_at.strftime("%Y-%m-%d %H:%M:%S") if user.created_at else "",
                "last_active_at": user.last_active_at.strftime("%Y-%m-%d %H:%M:%S") if user.last_active_at else "",
            },
            "projects": {"total": len(projects), "recent": recent_projects},
            "assets": {
                "total": inventory["project_asset_count"],
                "uploaded_images": inventory["original_count"],
                "reference_images": inventory["reference_count"],
                "export_storage_mb": _mb(export_storage_bytes),
                "project_storage_mb": _mb(inventory["project_storage_size"]),
            },
            "task_stats": {
                "completed_tasks": completed_tasks,
                "operated_task_count": operated_task_count,
                "failed_tasks": failed_tasks,
                "active_tasks": active_tasks,
                "task_total": task_total,
                "task_completion_rate": task_completion_rate,
                "task_success_rate": task_success_rate,
                "task_failure_rate": task_failure_rate,
                "task_export_rate": task_export_rate,
                "task_efficiency": task_efficiency,
                "export_events": export_events,
                "unique_exported_photo_count": unique_exported_photo_count,
                "export_count": export_events,
                "daily": daily,
                "recent_logs": recent_logs,
            },
            "usage": {"active_days_7d": active_days, "monthly_usage_count": monthly_usage_count},
        },
    }


class UserProfileUpdateRequest(BaseModel):
    nickname: str


class ExportMetricRecordRequest(BaseModel):
    file_count: int = 1
    total_bytes: int = 0
    export_format: str = ""
    size_mode: str = ""
    project_id: int | None = None
    file_name: str = ""
    source_image_key: str = ""


@router.post("/record_export_metric")
async def record_export_metric(
    payload: ExportMetricRecordRequest,
    user: User = Depends(get_current_user),
):
    file_count = max(int(payload.file_count or 0), 0)
    total_bytes = max(int(payload.total_bytes or 0), 0)
    export_format = str(payload.export_format or "").strip()
    size_mode = str(payload.size_mode or "").strip()
    project_id = int(payload.project_id or 0) if payload.project_id else 0
    file_name = str(payload.file_name or "").strip()
    source_image_key = str(payload.source_image_key or "").strip()

    if file_count <= 0 and total_bytes <= 0:
        return {"ok": True}

    if file_count > 0:
        record_export(file_count)
    record_user_usage(user.id)
    record_task_log(
        {
            "task_id": uuid.uuid4().hex[:12],
            "task_type": "导出",
            "event_type": "result",
            "status": "ok",
            "summary": "图片导出完成",
            "detail": size_mode or export_format or "image_export",
            "user_id": user.id,
            "user_label": _derive_display_name(user),
            "role": user.role,
            "model": export_format,
            "meta": {
                "export_format": export_format,
                "size_mode": size_mode,
                "export_size_bytes": total_bytes,
                "export_file_count": file_count,
                "project_id": project_id,
                "file_name": file_name,
                "source_image_key": source_image_key,
                "source": "frontend_export",
            },
        }
    )
    return {"ok": True}


@router.post("/space_profile")
async def update_user_space_profile(
    payload: UserProfileUpdateRequest,
    user: User = Depends(get_current_user),
):
    nickname = str(payload.nickname or "").strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="昵称不能为空")
    if len(nickname) > 24:
        raise HTTPException(status_code=400, detail="昵称不能超过 24 个字符")

    store = _read_user_profile_store()
    key = str(user.id)
    record = store.get(key) if isinstance(store.get(key), dict) else {}
    record["nickname"] = nickname
    record["updated_at"] = datetime.utcnow().isoformat() + "Z"
    store[key] = record
    _write_user_profile_store(store)
    return {
        "nickname": nickname,
        "avatar_url": _user_avatar_url(record),
        "updated_at": record["updated_at"],
    }


@router.put("/space_profile")
async def update_user_space_profile_put(
    payload: UserProfileUpdateRequest,
    user: User = Depends(get_current_user),
):
    return await update_user_space_profile(payload, user)


@router.post("/space_profile/avatar")
async def upload_user_space_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    ensure_upload_file_size(file, USER_PROFILE_MAX_AVATAR_BYTES, label="头像")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in USER_PROFILE_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="头像仅支持 jpg、jpeg、png、webp")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="头像文件为空")
    if len(content) > USER_PROFILE_MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="头像大小不能超过 2MB")

    USER_PROFILE_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    avatar_name = f"user_{user.id}_{uuid.uuid4().hex}{ext}"
    avatar_path = USER_PROFILE_AVATAR_DIR / avatar_name
    avatar_path.write_bytes(content)

    store = _read_user_profile_store()
    key = str(user.id)
    record = store.get(key) if isinstance(store.get(key), dict) else {}
    old_avatar_path = _resolve_existing_path(record.get("avatar_path"))
    if old_avatar_path and old_avatar_path.exists() and old_avatar_path != avatar_path:
        try:
            old_avatar_path.unlink()
        except OSError:
            pass

    record["avatar_path"] = f"/api/user_assets/profiles/avatars/{avatar_name}"
    record["updated_at"] = datetime.utcnow().isoformat() + "Z"
    store[key] = record
    _write_user_profile_store(store)
    return {
        "avatar_url": _user_avatar_url(record),
        "updated_at": record["updated_at"],
    }


class RateAssetRequest(BaseModel):
    file_name: str
    rating: int


@router.post("/{project_id}/rate_asset")
async def rate_asset(
    project_id: int,
    req: RateAssetRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.rating < 1 or req.rating > 5:
        raise HTTPException(status_code=400, detail="评分必须在 1-5 之间")

    proj_result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    if not proj_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="项目不存在")

    result = await db.execute(
        select(Asset).where(
            Asset.project_id == project_id,
            Asset.file_name == req.file_name,
        )
    )
    asset = result.scalar_one_or_none()

    if asset:
        asset.rating = req.rating
    else:
        asset = Asset(project_id=project_id, file_name=req.file_name, rating=req.rating)
        db.add(asset)

    await db.commit()
    return {"message": "评分已保存", "file_name": req.file_name, "rating": req.rating}


class SnapshotRequest(BaseModel):
    snapshot: str  # JSON string


class DeleteProjectAssetsRequest(BaseModel):
    paths: list[str]


@router.put("/{project_id}/snapshot")
async def save_snapshot(
    project_id: int,
    req: SnapshotRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project.workspace_snapshot = req.snapshot
    await db.commit()
    return {"message": "快照已保存"}


class RenameProjectRequest(BaseModel):
    name: str


@router.put("/{project_id}")
async def rename_project(
    project_id: int,
    req: RenameProjectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    project.name = req.name or "未命名项目"
    await db.commit()
    return {"message": "重命名成功", "name": project.name}


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    project.deleted_at = datetime.utcnow()
    await db.commit()
    return {"message": "已移入回收站", "deleted_at": str(project.deleted_at)}


@router.get("/trash")
async def list_trash(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.utcnow() - timedelta(days=30)
    expired = await db.execute(
        select(Project).where(
            Project.owner_id == user.id,
            Project.deleted_at != None,
            Project.deleted_at < cutoff,
        )
    )
    for p in expired.scalars().all():
        await _purge_project_resources(db, p.id)
        await db.delete(p)
    await db.commit()

    result = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id, Project.deleted_at != None)
        .order_by(desc(Project.deleted_at))
    )
    projects = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "type": p.type,
            "owner_id": p.owner_id,
            "created_at": str(p.created_at) if p.created_at else "",
            "deleted_at": str(p.deleted_at) if p.deleted_at else "",
        }
        for p in projects
    ]


@router.put("/{project_id}/restore")
async def restore_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    project.deleted_at = None
    await db.commit()
    return {"message": "项目已恢复"}


@router.delete("/{project_id}/permanent")
async def permanent_delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    await _purge_project_resources(db, project.id)
    await db.delete(project)
    await db.commit()
    return {"message": "永久删除成功"}


@router.delete("/trash/empty")
async def empty_trash(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    trashed_result = await db.execute(
        select(Project.id).where(
            Project.owner_id == user.id, Project.deleted_at != None
        )
    )
    for row in trashed_result.all():
        await _purge_project_resources(db, row[0])
    await db.execute(
        delete(Project).where(
            Project.owner_id == user.id, Project.deleted_at != None
        )
    )
    await db.commit()
    return {"message": "回收站已清空"}


@router.delete("/{project_id}/assets")
async def delete_project_assets(
    project_id: int,
    req: DeleteProjectAssetsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="项目不存在")

    raw_paths = req.paths if isinstance(req.paths, list) else []
    normalized_paths = []
    seen = set()
    for item in raw_paths:
        raw = str(item or "").strip()
        if not raw:
            continue
        normalized = raw.split("?", 1)[0].replace("\\", "/")
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_paths.append(normalized)

    if not normalized_paths:
        return {"deleted": [], "missing": []}

    project_roots = list(_project_storage_roots(project_id))

    deleted = []
    missing = []
    asset_names = set()

    for raw in normalized_paths:
        candidate = _resolve_existing_path(raw)
        if candidate is None:
            missing.append(raw)
            continue

        try:
            resolved = candidate.resolve()
        except OSError:
            missing.append(raw)
            continue

        if not any(root == resolved or root in resolved.parents for root in project_roots):
            raise HTTPException(status_code=400, detail="仅允许删除当前项目目录内的文件")

        if not resolved.exists() or not resolved.is_file():
            missing.append(raw)
            continue

        asset_names.add(resolved.name)
        try:
            resolved.unlink()
            deleted.append(raw)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除文件失败: {resolved.name}") from exc

    if asset_names:
        await db.execute(
            delete(Asset).where(
                Asset.project_id == project_id,
                Asset.file_name.in_(list(asset_names))
            )
        )
    await db.commit()
    return {"deleted": deleted, "missing": missing}


@router.post("/{project_id}/upload")
async def upload_project_asset(
    project_id: int,
    file: UploadFile = File(...),
    bucket: str = Form("source"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="项目不存在")

    safe_bucket = re.sub(r"[^A-Za-z0-9._-]+", "_", str(bucket or "source")).strip("._-") or "source"
    ensure_upload_file_size(file, 300 * 1024 * 1024 if safe_bucket.startswith("video") else 10 * 1024 * 1024, label="项目文件")
    proj_dir = _project_assets_root() / str(project_id) / safe_bucket
    proj_dir.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(file.filename)[1] if file.filename else ""
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = proj_dir / fname
    with open(fpath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    asset_url = f"/api/project_assets/{project_id}/{safe_bucket}/{fname}"
    return {
        "path": str(fpath).replace("\\", "/"),
        "asset_url": asset_url,
        "filename": file.filename,
    }
