import os
import json
import re
import shutil
import time
import ctypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_runtime_metrics import get_monthly_user_usage, get_task_logs, load_runtime_stats
from app.routes.auth import require_admin
from app.routes.admin_models import _build_status_payload
from app.routes.projects import _derive_display_name, _user_profile_record
from config import (
    BASE_DIR,
    MODEL_DIR,
    MODFLOWS_B0_CHECKPOINT,
    MODFLOWS_B6_CHECKPOINT,
    STORAGE_CACHE_DIR,
    STORAGE_IMAGE_DEBUG_DIR,
    STORAGE_IMAGE_UPLOADS_DIR,
    STORAGE_LOGS_DIR,
    STORAGE_PROJECTS_DIR,
    STORAGE_TRAINING_CORPUS_DIR,
    STORAGE_USERS_DIR,
    STORAGE_VIDEO_FRAMES_DIR,
    STORAGE_VIDEO_UPLOADS_DIR,
    STORAGE_VIDEOS_DIR,
    get_neuralpreset_weight_status,
)
from database import get_db
from models import Asset, Project, User
from progress import progress_manager
from scripts.backfill_admin_task_logs import backfill_admin_task_logs

router = APIRouter()

TRAINING_DATA_ROOT = STORAGE_TRAINING_CORPUS_DIR
TRAINING_CORPUS_ROOT = STORAGE_TRAINING_CORPUS_DIR
ADMIN_SNAPSHOT_PATH = STORAGE_CACHE_DIR / "admin_metrics_snapshot.json"
ADMIN_WEEKLY_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
ADMIN_WEEKLY_REFRESH_HOUR = 8
ADMIN_AUTO_REFRESH_SECONDS = 60
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TASK_LOG_USER_KEY_PATTERN = re.compile(r"user_(\d+)_(?:admin|user)$", re.IGNORECASE)
HF_MODEL_CACHE_DIR_NAMES = (
    "models--ZhengPeng7--BiRefNet",
    "models--facebook--dinov2-small",
)


def _scan_dir(path: Path, extensions=None):
    stats = {"file_count": 0, "size_bytes": 0}
    if not path.exists():
        return stats

    allowed = {ext.lower() for ext in extensions} if extensions else None
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        if allowed and item.suffix.lower() not in allowed:
            continue
        try:
            stats["file_count"] += 1
            stats["size_bytes"] += item.stat().st_size
        except OSError:
            continue
    return stats


def _scan_paths_unique(paths, extensions=None):
    stats = {"file_count": 0, "size_bytes": 0}
    seen = set()
    allowed = {ext.lower() for ext in extensions} if extensions else None

    def add_file(file_path: Path):
        if allowed and file_path.suffix.lower() not in allowed:
            return
        try:
            resolved = file_path.resolve()
            if resolved in seen:
                return
            seen.add(resolved)
            stats["file_count"] += 1
            stats["size_bytes"] += resolved.stat().st_size
        except OSError:
            return

    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        if path.is_file():
            add_file(path)
            continue
        for item in path.rglob("*"):
            if item.is_file():
                add_file(item)
    return stats


def _hf_model_cache_roots():
    hub_dir = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    return [hub_dir / name for name in HF_MODEL_CACHE_DIR_NAMES]


def _scan_training_corpus(path: Path):
    stats = {
        "user_count": 0,
        "sample_count": 0,
        "file_count": 0,
        "size_bytes": 0,
        "target_count": 0,
        "reference_count": 0,
        "result_count": 0,
        "meta_count": 0,
        "rating_count": 0,
        "data_types": [],
    }
    if not path.exists():
        return stats

    user_dirs = [item for item in path.iterdir() if item.is_dir()]
    stats["user_count"] = len(user_dirs)
    for user_dir in user_dirs:
        for sample_dir in user_dir.iterdir():
            if not sample_dir.is_dir():
                continue
            stats["sample_count"] += 1
            for item in sample_dir.iterdir():
                if not item.is_file():
                    continue
                lower_name = item.name.lower()
                stats["file_count"] += 1
                try:
                    stats["size_bytes"] += item.stat().st_size
                except OSError:
                    pass
                if lower_name.startswith("target."):
                    stats["target_count"] += 1
                elif lower_name.startswith("reference."):
                    stats["reference_count"] += 1
                elif lower_name.startswith("result."):
                    stats["result_count"] += 1
                elif lower_name == "meta.json":
                    stats["meta_count"] += 1
                    try:
                        meta = json.loads(item.read_text(encoding="utf-8"))
                        if int(meta.get("rating") or 0) > 0:
                            stats["rating_count"] += 1
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        pass
    stats["data_types"] = [
        {
            "label": "原图 target",
            "count": stats["target_count"],
            "unit": "个",
            "ready": stats["target_count"] > 0,
        },
        {
            "label": "参考图 reference",
            "count": stats["reference_count"],
            "unit": "个",
            "ready": stats["reference_count"] > 0,
        },
        {
            "label": "导出结果 result",
            "count": stats["result_count"],
            "unit": "个",
            "ready": stats["result_count"] > 0,
        },
        {
            "label": "满意度评分 rating",
            "count": stats["rating_count"],
            "unit": "条",
            "ready": stats["rating_count"] > 0,
        },
        {
            "label": "导出参数 meta",
            "count": stats["meta_count"],
            "unit": "个",
            "ready": stats["meta_count"] > 0,
        },
    ]
    return stats


def _mb(size_bytes):
    return round(size_bytes / 1024 / 1024, 2)


def _format_size_mb(size_mb):
    try:
        value = float(size_mb or 0)
    except (TypeError, ValueError):
        value = 0.0
    if abs(value) >= 1024:
        return f"{value / 1024:.2f} GB"
    return f"{value:.1f} MB"


def _read_admin_snapshot():
    if not ADMIN_SNAPSHOT_PATH.exists():
        return {}
    try:
        with open(ADMIN_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _admin_week_start(now=None):
    now = now or datetime.now(ADMIN_WEEKLY_TZ)
    monday = now - timedelta(days=now.weekday())
    boundary = monday.replace(
        hour=ADMIN_WEEKLY_REFRESH_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if now < boundary:
        boundary -= timedelta(days=7)
    return boundary


def _admin_week_key(dt):
    return dt.strftime("%Y-%m-%d")


def _weekly_snapshot_context(snapshot):
    week_start = _admin_week_start()
    previous_week_start = week_start - timedelta(days=7)
    weekly = snapshot.get("weekly_metrics", {})
    if not isinstance(weekly, dict):
        weekly = {}
    return {
        "week_key": _admin_week_key(week_start),
        "previous_week_key": _admin_week_key(previous_week_start),
        "previous": weekly.get(_admin_week_key(previous_week_start), {}),
        "weekly": weekly,
    }


def _write_admin_snapshot(metrics, weekly_context=None):
    ADMIN_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    weekly_context = weekly_context or _weekly_snapshot_context({})
    weekly = dict(weekly_context.get("weekly") or {})
    weekly[weekly_context["week_key"]] = metrics
    weekly = {key: weekly[key] for key in sorted(weekly.keys())[-12:]}
    with open(ADMIN_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "metrics": metrics,
                "saved_at": datetime.utcnow().isoformat() + "Z",
                "weekly_metrics": weekly,
                "weekly_period": weekly_context["week_key"],
                "compare_period": weekly_context["previous_week_key"],
                "weekly_refresh_rule": "Asia/Shanghai 每周一 08:00",
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def _delta(current, previous):
    if previous is None:
        return None
    try:
        return round(float(current) - float(previous), 1)
    except (TypeError, ValueError):
        return None


def _model_ready_rate(ready_models, total_models):
    return round(ready_models / max(total_models, 1) * 100, 1)


def _current_model_summary():
    payload = _build_status_payload()
    summary = payload.get("summary") or {}
    models = payload.get("models") or []
    total = int(summary.get("total") or len(models))
    ready = int(
        summary.get("ready")
        if summary.get("ready") is not None
        else sum(1 for item in models if item.get("ready") and item.get("status") == "ready")
    )
    installed_or_partial = int(
        summary.get("installed_or_partial")
        if summary.get("installed_or_partial") is not None
        else sum(1 for item in models if item.get("ready"))
    )
    missing = int(
        summary.get("missing")
        if summary.get("missing") is not None
        else max(total - installed_or_partial, 0)
    )
    return {
        "ready": ready,
        "total": total,
        "installed_or_partial": installed_or_partial,
        "missing": missing,
        "ready_rate": _model_ready_rate(ready, total),
    }


def _health_score(task_completion_rate, model_ready_rate, task_success_rate):
    score = (
        float(task_completion_rate or 0) * 0.35
        + float(model_ready_rate or 0) * 0.30
        + float(task_success_rate or 0) * 0.35
    )
    return round(score, 1)


def _format_delta_text(delta, unit="", precision=1):
    if delta is None:
        return None
    try:
        value = round(float(delta), precision)
    except (TypeError, ValueError):
        return None
    sign = "+" if value > 0 else ""
    formatted = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return f"{sign}{formatted}{unit}"


def _metric_card(key, label, value, unit, previous, spark_seed=None, series=None, display_value=None):
    prev = previous.get(key) if isinstance(previous, dict) else None
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "display_value": display_value,
        "delta": _delta(value, prev),
        "sparkline": series or [],
        "spark_seed": spark_seed,
    }


def _runtime_task_stats(runtime_stats):
    tasks = runtime_stats.get("tasks", {})
    exports = runtime_stats.get("exports", {})
    model_calls = runtime_stats.get("model_calls", {})
    return {
        "completed_tasks": int(tasks.get("success", 0)),
        "failed_tasks": int(tasks.get("failed", 0)),
        "active_tasks": int(progress_manager.active_task_count()),
        "total_export_events": int(exports.get("count", 0)),
        "model_calls_total": int(model_calls.get("total", 0)),
        "model_calls_breakdown": {
            "neural_preset": int(model_calls.get("neural_preset", 0)),
            "modflows_b0": int(model_calls.get("modflows_b0", 0)),
            "modflows_b6": int(model_calls.get("modflows_b6", 0)),
        },
    }


def _daily_series(runtime_stats):
    daily = runtime_stats.get("daily", {})
    today = datetime.now(ADMIN_WEEKLY_TZ).date()
    result = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_key = day.strftime("%Y-%m-%d")
        bucket = daily.get(day_key, {})
        result.append(
            {
                "date": day_key,
                "label": WEEKDAY_LABELS[day.weekday()],
                "tasks": int(bucket.get("task_success", 0)),
                "exports": int(bucket.get("exports", 0)),
                "model_calls": int(bucket.get("model_calls", 0)),
            }
        )
    return result


def _disk_usage_stats(path: Path):
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    used_percent = round(used / usage.total * 100, 1) if usage.total else 0
    return {
        "used_bytes": used,
        "total_bytes": usage.total,
        "used_mb": _mb(used),
        "total_mb": _mb(usage.total),
        "used_percent": used_percent,
    }


def _memory_usage_stats():
    try:
        import psutil

        memory = psutil.virtual_memory()
        return {
            "used_percent": round(float(memory.percent or 0), 1),
            "used_mb": _mb(memory.used),
            "total_mb": _mb(memory.total),
        }
    except Exception:
        if ctypes and hasattr(ctypes, "windll") and os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            memory_status = MEMORYSTATUSEX()
            memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
                used_bytes = max(int(memory_status.ullTotalPhys or 0) - int(memory_status.ullAvailPhys or 0), 0)
                return {
                    "used_percent": round(float(memory_status.dwMemoryLoad or 0), 1),
                    "used_mb": _mb(used_bytes),
                    "total_mb": _mb(int(memory_status.ullTotalPhys or 0)),
                }
        return {
            "used_percent": None,
            "used_mb": None,
            "total_mb": None,
        }


def _cpu_used_percent():
    try:
        import psutil
        return round(float(psutil.cpu_percent(interval=0.08) or 0), 1)
    except Exception:
        if not (hasattr(ctypes, "windll") and os.name == "nt"):
            return None

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

        def _read_times():
            idle_time = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            return (
                (idle_time.dwHighDateTime << 32) | idle_time.dwLowDateTime,
                (kernel_time.dwHighDateTime << 32) | kernel_time.dwLowDateTime,
                (user_time.dwHighDateTime << 32) | user_time.dwLowDateTime,
            )

        first = _read_times()
        if not first:
            return None
        time.sleep(0.08)
        second = _read_times()
        if not second:
            return None
        idle_delta = second[0] - first[0]
        total_delta = (second[1] - first[1]) + (second[2] - first[2])
        if total_delta <= 0:
            return None
        return round(max(0.0, min(1.0, 1.0 - idle_delta / total_delta)) * 100, 1)


def _live_resource_snapshot(path: Path):
    disk_stats = _disk_usage_stats(path)
    memory_stats = _memory_usage_stats()
    return {
        "disk_used_percent": disk_stats.get("used_percent"),
        "memory_used_percent": memory_stats.get("used_percent"),
        "cpu_used_percent": _cpu_used_percent(),
    }


def _append_user_lookup_candidate(candidates, value):
    raw = str(value or "").strip().lower()
    if not raw:
        return
    if raw not in candidates:
        candidates.append(raw)
    match = TASK_LOG_USER_KEY_PATTERN.fullmatch(raw)
    if match:
        user_id = match.group(1)
        if user_id and user_id not in candidates:
            candidates.append(user_id)


def _format_admin_meta_value(key, value):
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


def _build_admin_meta_display(meta):
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
            "value": _format_admin_meta_value(str(key), value),
        })
    return items


def _build_admin_display(entry, meta):
    summary = str(entry.get("summary") or "").strip()
    display = entry.get("display") if isinstance(entry.get("display"), dict) else {}
    detail = str(display.get("detail") or entry.get("detail") or "").strip()
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
    if detail in detail_map:
        detail = detail_map[detail]
    if not detail:
        source = str(meta.get("source") or "").strip()
        detail = detail_map.get(source, summary)
    return {
        "summary": str(display.get("summary") or summary),
        "detail": detail,
    }


def _standardize_admin_log_entry(entry, user_map, live_resource_snapshot):
    if not isinstance(entry, dict):
        return {}

    normalized = dict(entry)
    meta = normalized.get("meta_raw")
    if not isinstance(meta, dict):
        meta = normalized.get("meta") if isinstance(normalized.get("meta"), dict) else {}
    meta = dict(meta)

    user_payload = normalized.get("user") if isinstance(normalized.get("user"), dict) else {}
    user_id = normalized.get("user_id", user_payload.get("id"))
    candidate_keys = []
    if user_id is not None:
        _append_user_lookup_candidate(candidate_keys, user_id)
    for value in (
        user_payload.get("email"),
        user_payload.get("display_name"),
        normalized.get("email"),
        normalized.get("user_label"),
        meta.get("email"),
        meta.get("user_email"),
        meta.get("user_account"),
    ):
        _append_user_lookup_candidate(candidate_keys, value)

    matched_user = None
    for key in candidate_keys:
        matched_user = user_map.get(key)
        if matched_user:
            break

    if matched_user:
        display_name = _derive_display_name(matched_user)
        profile_record = _user_profile_record(matched_user.id)
        user_payload = {
            "id": matched_user.id,
            "display_name": display_name or matched_user.email or f"用户{matched_user.id}",
            "email": str(matched_user.email or meta.get("user_email") or meta.get("email") or "").strip(),
            "role": str(matched_user.role or normalized.get("role") or "user"),
            "avatar_url": str((profile_record or {}).get("avatar_path") or normalized.get("avatar_url") or "").strip(),
        }
    else:
        fallback_name = str(
            user_payload.get("display_name")
            or normalized.get("user_label")
            or meta.get("user_account")
            or "游客"
        ).strip()
        user_payload = {
            "id": user_id if isinstance(user_id, int) else None,
            "display_name": fallback_name or "游客",
            "email": str(
                user_payload.get("email")
                or normalized.get("email")
                or meta.get("user_email")
                or meta.get("email")
                or ""
            ).strip(),
            "role": str(user_payload.get("role") or normalized.get("role") or "user"),
            "avatar_url": str(user_payload.get("avatar_url") or normalized.get("avatar_url") or "").strip(),
        }

    meta.setdefault("user_email", user_payload.get("email") or "")
    meta.setdefault("user_account", user_payload.get("display_name") or "")
    display_payload = _build_admin_display(normalized, meta)
    meta_display = normalized.get("meta_display")
    if not isinstance(meta_display, list) or not meta_display:
        meta_display = _build_admin_meta_display(meta)

    timing = normalized.get("timing") if isinstance(normalized.get("timing"), dict) else {}
    if "duration_ms" not in timing:
        timing = {"duration_ms": normalized.get("duration_ms")}

    resource = normalized.get("resource") if isinstance(normalized.get("resource"), dict) else {}
    if not resource or all(resource.get(key) in (None, "", 0) for key in ("disk_used_percent", "memory_used_percent", "cpu_used_percent")):
        resource = dict(live_resource_snapshot)
        normalized["resource_live_fallback"] = True

    normalized["meta_raw"] = meta
    normalized["meta"] = meta
    normalized["display"] = display_payload
    normalized["summary"] = display_payload.get("summary") or normalized.get("summary") or ""
    normalized["detail"] = display_payload.get("detail") or normalized.get("detail") or ""
    normalized["user"] = user_payload
    normalized["user_id"] = user_payload.get("id")
    normalized["user_label"] = user_payload.get("display_name")
    normalized["email"] = user_payload.get("email")
    normalized["role"] = user_payload.get("role")
    normalized["avatar_url"] = user_payload.get("avatar_url")
    normalized["timing"] = timing
    normalized["duration_ms"] = timing.get("duration_ms")
    normalized["meta_display"] = meta_display
    normalized["resource"] = resource
    normalized["schema_version"] = int(normalized.get("schema_version") or 2)
    return normalized


def _build_admin_alerts(disk_stats, memory_stats, active_tasks, cpu_percent=None):
    alerts = []
    disk_percent = disk_stats.get("used_percent") if isinstance(disk_stats, dict) else None
    memory_percent = memory_stats.get("used_percent") if isinstance(memory_stats, dict) else None

    if disk_percent is not None and disk_percent >= 90:
        alerts.append({
            "level": "danger",
            "type": "disk",
            "title": "硬盘空间告警",
            "message": f"系统盘使用率 {disk_percent:.1f}%，建议立即清理训练输出与导出缓存。",
        })
    elif disk_percent is not None and disk_percent >= 80:
        alerts.append({
            "level": "warn",
            "type": "disk",
            "title": "硬盘空间偏高",
            "message": f"系统盘使用率 {disk_percent:.1f}%，建议关注缓存与导出目录增长。",
        })

    if memory_percent is not None and memory_percent >= 90:
        alerts.append({
            "level": "danger",
            "type": "memory",
            "title": "内存占用过高",
            "message": f"当前内存占用 {memory_percent:.1f}%，建议暂停高负载任务。",
        })
    elif memory_percent is not None and memory_percent >= 80:
        alerts.append({
            "level": "warn",
            "type": "memory",
            "title": "内存压力偏高",
            "message": f"当前内存占用 {memory_percent:.1f}%，建议暂时限制视频任务并发。",
        })

    if cpu_percent is not None and cpu_percent >= 90:
        alerts.append({
            "level": "danger",
            "type": "cpu",
            "title": "CPU 占用过高",
            "message": f"当前 CPU 占用 {cpu_percent:.1f}%，建议暂停训练或批量任务。",
        })
    elif cpu_percent is not None and cpu_percent >= 80:
        alerts.append({
            "level": "warn",
            "type": "cpu",
            "title": "CPU 压力偏高",
            "message": f"当前 CPU 占用 {cpu_percent:.1f}%，建议关注并发任务与导出负载。",
        })

    if int(active_tasks or 0) >= 2:
        alerts.append({
            "level": "info",
            "type": "active_tasks",
            "title": "任务并发提醒",
            "message": f"当前有 {int(active_tasks)} 个活跃任务，建议避免训练和批量导出叠加。",
        })

    return alerts


def _overview_to_metrics(data):
    return {
        "users": data["users"]["total"],
        "active_users_7d": data["users"]["active_7d"],
        "monthly_power_users": data["users"]["monthly_power_users"],
        "monthly_power_user_ratio": data["users"]["monthly_power_user_ratio"],
        "projects": data["projects"]["total"],
        "active_projects": data["projects"]["active"],
        "assets": data["projects"]["assets"],
        "storage_used_mb": data["storage"]["project_storage_mb"],
        "system_disk_used_percent": data["storage"]["system_disk_used_percent"],
        "models_mb": data["model_data"].get(
            "model_total_size_mb",
            round(
                data["model_data"]["model_size_mb"] + data["model_data"]["weight_size_mb"],
                2,
            ),
        ),
        "ready_models": data["model_data"]["ready_models"],
        "total_models": data["model_data"]["total_models"],
        "model_calls_total": data["task_stats"]["model_calls_total"],
        "training_files": data["model_data"]["training_file_count"],
        "training_mb": data["model_data"]["training_size_mb"],
        "training_corpus_samples": data["model_data"]["training_corpus_sample_count"],
        "training_corpus_files": data["model_data"]["training_corpus_file_count"],
        "training_corpus_mb": data["model_data"]["training_corpus_size_mb"],
        "training_corpus_users": data["model_data"]["training_corpus_user_count"],
        "completed_tasks": data["task_stats"]["completed_tasks"],
        "active_tasks": data["task_stats"]["active_tasks"],
        "failed_tasks": data["task_stats"]["failed_tasks"],
        "exports": data["task_stats"]["total_export_files"],
        "export_events": data["task_stats"]["total_export_events"],
        "export_mb": data["task_stats"]["export_size_mb"],
        "task_total": data["task_stats"]["task_total"],
        "task_success": data["task_stats"]["task_success"],
        "task_completion_rate": data["task_stats"]["task_completion_rate"],
        "task_success_rate": data["task_stats"]["task_success_rate"],
        "task_export_rate": data["task_stats"]["task_export_rate"],
        "task_failure_rate": data["task_stats"]["task_failure_rate"],
    }


async def _collect_overview(db: AsyncSession):
    user_count = await db.scalar(select(func.count(User.id)))
    active_cutoff = datetime.utcnow() - timedelta(days=7)
    active_user_count = await db.scalar(
        select(func.count(User.id)).where(
            User.last_active_at != None,
            User.last_active_at >= active_cutoff,
        )
    )
    admin_count = await db.scalar(select(func.count(User.id)).where(User.role == "admin"))
    project_count = await db.scalar(select(func.count(Project.id)))
    active_project_count = await db.scalar(
        select(func.count(Project.id)).where(Project.deleted_at == None)
    )
    asset_count = await db.scalar(select(func.count(Asset.id)))

    model_stats = _scan_dir(MODEL_DIR)
    weights_stats = _scan_dir(BASE_DIR / "weights")
    hf_model_cache_stats = _scan_paths_unique(_hf_model_cache_roots())
    model_total_stats = _scan_paths_unique(
        [MODEL_DIR, BASE_DIR / "weights", *_hf_model_cache_roots()]
    )
    train_stats = _scan_paths_unique([TRAINING_DATA_ROOT, BASE_DIR / "temp_train_data"])
    legacy_training_stats = _scan_dir(BASE_DIR / "temp_train_data")
    training_corpus_stats = _scan_training_corpus(TRAINING_CORPUS_ROOT)
    export_stats = _scan_paths_unique(
        [STORAGE_VIDEOS_DIR, BASE_DIR / "videos"],
        extensions={
            ".jpg",
            ".jpeg",
            ".png",
            ".tif",
            ".tiff",
            ".webp",
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
        },
    )
    upload_stats = _scan_dir(STORAGE_IMAGE_UPLOADS_DIR)
    style_stats = _scan_dir(BASE_DIR / "styles")
    asset_storage_stats = _scan_dir(STORAGE_USERS_DIR)
    storage_stats = _scan_paths_unique(
        [
            STORAGE_PROJECTS_DIR,
            STORAGE_USERS_DIR,
            STORAGE_IMAGE_UPLOADS_DIR,
            STORAGE_VIDEO_UPLOADS_DIR,
            STORAGE_VIDEOS_DIR,
            STORAGE_IMAGE_DEBUG_DIR,
            STORAGE_VIDEO_FRAMES_DIR,
            STORAGE_LOGS_DIR,
            STORAGE_TRAINING_CORPUS_DIR,
        ]
    )
    runtime_stats = load_runtime_stats()
    monthly_usage = get_monthly_user_usage(runtime_stats)
    monthly_power_users = sum(1 for count in monthly_usage.values() if count > 100)
    monthly_power_user_ratio = (
        0
        if not (user_count or 0)
        else round(monthly_power_users / max(user_count, 1) * 100, 1)
    )
    runtime_task_stats = _runtime_task_stats(runtime_stats)
    daily_stats = _daily_series(runtime_stats)

    model_summary = _current_model_summary()
    ready_models = model_summary["ready"]
    total_models = model_summary["total"]
    task_total = runtime_task_stats["completed_tasks"] + runtime_task_stats["failed_tasks"]
    task_success = runtime_task_stats["completed_tasks"]
    task_closure_base = (
        runtime_task_stats["completed_tasks"]
        + runtime_task_stats["failed_tasks"]
        + runtime_task_stats["active_tasks"]
    )
    task_completion_rate = (
        0
        if task_closure_base <= 0
        else round(runtime_task_stats["completed_tasks"] / task_closure_base * 100, 1)
    )
    task_success_rate = (
        0
        if task_total <= 0
        else round(runtime_task_stats["completed_tasks"] / task_total * 100, 1)
    )
    task_export_rate = (
        0
        if task_success <= 0
        else round(
            min(runtime_task_stats["total_export_events"], task_success) / task_success * 100,
            1,
        )
    )
    task_failure_rate = (
        0
        if task_total <= 0
        else round(runtime_task_stats["failed_tasks"] / task_total * 100, 1)
    )

    disk_stats = _disk_usage_stats(BASE_DIR)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "users": {
            "total": user_count or 0,
            "active_7d": active_user_count or 0,
            "monthly_power_users": monthly_power_users,
            "monthly_power_user_ratio": monthly_power_user_ratio,
            "admins": admin_count or 0,
        },
        "projects": {
            "total": project_count or 0,
            "active": active_project_count or 0,
            "assets": asset_count or 0,
        },
        "storage": {
            "project_storage_mb": _mb(storage_stats["size_bytes"]),
            "project_file_count": storage_stats["file_count"],
            "system_disk_used_percent": disk_stats["used_percent"],
            "system_disk_used_mb": disk_stats["used_mb"],
            "system_disk_total_mb": disk_stats["total_mb"],
        },
        "model_data": {
            "model_file_count": model_stats["file_count"],
            "model_size_mb": _mb(model_stats["size_bytes"]),
            "weight_file_count": weights_stats["file_count"],
            "weight_size_mb": _mb(weights_stats["size_bytes"]),
            "model_cache_file_count": hf_model_cache_stats["file_count"],
            "model_cache_size_mb": _mb(hf_model_cache_stats["size_bytes"]),
            "model_total_file_count": model_total_stats["file_count"],
            "model_total_size_mb": _mb(model_total_stats["size_bytes"]),
            "ready_models": ready_models,
            "total_models": total_models,
            "training_file_count": train_stats["file_count"],
            "training_size_mb": _mb(train_stats["size_bytes"]),
            "legacy_training_file_count": legacy_training_stats["file_count"],
            "legacy_training_size_mb": _mb(legacy_training_stats["size_bytes"]),
            "training_corpus_user_count": training_corpus_stats["user_count"],
            "training_corpus_sample_count": training_corpus_stats["sample_count"],
            "training_corpus_file_count": training_corpus_stats["file_count"],
            "training_corpus_size_mb": _mb(training_corpus_stats["size_bytes"]),
            "training_corpus_target_count": training_corpus_stats["target_count"],
            "training_corpus_reference_count": training_corpus_stats["reference_count"],
            "training_corpus_result_count": training_corpus_stats["result_count"],
            "training_corpus_meta_count": training_corpus_stats["meta_count"],
            "training_corpus_rating_count": training_corpus_stats["rating_count"],
            "training_corpus_data_types": training_corpus_stats["data_types"],
        },
        "task_stats": {
            "completed_tasks": runtime_task_stats["completed_tasks"],
            "active_tasks": runtime_task_stats["active_tasks"],
            "failed_tasks": runtime_task_stats["failed_tasks"],
            "task_total": task_total,
            "task_success": task_success,
            "task_completion_rate": task_completion_rate,
            "task_success_rate": task_success_rate,
            "task_export_rate": task_export_rate,
            "task_failure_rate": task_failure_rate,
            "total_export_files": export_stats["file_count"],
            "total_export_events": runtime_task_stats["total_export_events"],
            "export_size_mb": _mb(export_stats["size_bytes"]),
            "model_calls_total": runtime_task_stats["model_calls_total"],
            "model_calls_breakdown": runtime_task_stats["model_calls_breakdown"],
            "daily": daily_stats,
        },
    }


@router.get("/overview")
async def admin_overview(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    return await _collect_overview(db)


@router.get("/dashboard")
async def admin_dashboard(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    overview = await _collect_overview(db)
    current = _overview_to_metrics(overview)
    snapshot = _read_admin_snapshot()
    weekly_context = _weekly_snapshot_context(snapshot)
    previous = weekly_context["previous"]
    daily = overview["task_stats"]["daily"]

    cards = [
        _metric_card("users", "注册用户", current["users"], "人", previous, 3),
        _metric_card(
            "completed_tasks",
            "完成任务",
            current["completed_tasks"],
            "个",
            previous,
            5,
            [item["tasks"] for item in daily] + [current["completed_tasks"]],
        ),
        _metric_card(
            "models_mb",
            "模型数据",
            current["models_mb"],
            "MB",
            previous,
            7,
            display_value=_format_size_mb(current["models_mb"]),
        ),
        _metric_card(
            "exports",
            "导出文件",
            current["exports"],
            "个",
            previous,
            11,
            [item["exports"] for item in daily] + [current["exports"]],
        ),
    ]

    model_ready_rate = _model_ready_rate(current["ready_models"], current["total_models"])
    health_score = _health_score(
        current["task_completion_rate"],
        model_ready_rate,
        current["task_success_rate"],
    )

    rings = [
        {
            "label": "任务完成率",
            "value": current["completed_tasks"],
            "percent": current["task_completion_rate"],
            "delta": _delta(
                current["task_completion_rate"],
                previous.get("task_completion_rate")
                if "task_completion_rate" in previous
                else None,
            ),
            "color": "#3557f2",
        },
        {
            "label": "模型就绪率",
            "value": current["ready_models"],
            "percent": model_ready_rate,
            "delta": _delta(
                model_ready_rate,
                _model_ready_rate(previous.get("ready_models", 0), current["total_models"])
                if "ready_models" in previous
                else None,
            ),
            "color": "#4866ff",
        },
        {
            "label": "任务成功率",
            "value": current["task_success"],
            "percent": current["task_success_rate"],
            "delta": _delta(
                current["task_success_rate"],
                previous.get("task_success_rate")
                if "task_success_rate" in previous
                else None,
            ),
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
            "label": "近 7 日活跃用户占比",
            "value": round(current["active_users_7d"] / max(current["users"], 1) * 100, 1),
            "delta": _delta(
                current["active_users_7d"],
                previous.get("active_users_7d") if "active_users_7d" in previous else None,
            ),
            "delta_unit": "人",
            "color": "#4658f5",
        },
        {
            "label": "月深度用户占比",
            "value": current["monthly_power_user_ratio"],
            "delta": _delta(
                current["monthly_power_user_ratio"],
                previous.get("monthly_power_user_ratio")
                if "monthly_power_user_ratio" in previous
                else None,
            ),
            "delta_unit": "%",
            "color": "#26b86f",
        },
        {
            "label": "任务导出率",
            "value": current["task_export_rate"],
            "delta": _delta(
                current["task_export_rate"],
                previous.get("task_export_rate") if "task_export_rate" in previous else None,
            ),
            "delta_unit": "%",
            "color": "#8a66dd",
        },
        {
            "label": "系统资源利用率",
            "value": current["system_disk_used_percent"],
            "delta": _delta(
                current["system_disk_used_percent"],
                previous.get("system_disk_used_percent")
                if "system_disk_used_percent" in previous
                else None,
            ),
            "delta_unit": "%",
            "color": "#ff8c66",
        },
    ]

    categories = [
        {
            "label": "存储使用",
            "value": _format_size_mb(current["storage_used_mb"]),
            "delta": _delta(
                current["storage_used_mb"],
                previous.get("storage_used_mb") if "storage_used_mb" in previous else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["storage_used_mb"],
                    previous.get("storage_used_mb")
                    if "storage_used_mb" in previous
                    else None,
                ),
                " MB",
            ),
            "color": "#4658f5",
        },
        {
            "label": "训练数据",
            "value": f"{current['training_files']} 张 / {current['training_mb']:.1f} MB",
            "delta": _delta(
                current["training_files"],
                previous.get("training_files") if "training_files" in previous else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["training_files"],
                    previous.get("training_files")
                    if "training_files" in previous
                    else None,
                ),
                " 张",
            ),
            "color": "#26b86f",
        },
        {
            "label": "训练样本副本",
            "value": f"{current['training_corpus_samples']} 组 / {current['training_corpus_mb']:.1f} MB",
            "delta": _delta(
                current["training_corpus_samples"],
                previous.get("training_corpus_samples")
                if "training_corpus_samples" in previous
                else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["training_corpus_samples"],
                    previous.get("training_corpus_samples")
                    if "training_corpus_samples" in previous
                    else None,
                ),
                " 组",
            ),
            "color": "#18a999",
        },
        {
            "label": "导出存储",
            "value": f"{current['exports']} 个 / {current['export_mb']:.1f} MB",
            "delta": _delta(
                current["export_mb"],
                previous.get("export_mb") if "export_mb" in previous else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["export_mb"],
                    previous.get("export_mb")
                    if "export_mb" in previous
                    else None,
                ),
                " MB",
            ),
            "color": "#f05f70",
        },
        {
            "label": "项目资产",
            "value": f"{current['assets']} 个",
            "delta": _delta(
                current["assets"],
                previous.get("assets") if "assets" in previous else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["assets"],
                    previous.get("assets") if "assets" in previous else None,
                ),
                " 个",
            ),
            "color": "#2aa9d6",
        },
        {
            "label": "模型调用次数",
            "value": f"{current['model_calls_total']} 次",
            "delta": _delta(
                current["model_calls_total"],
                previous.get("model_calls_total") if "model_calls_total" in previous else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["model_calls_total"],
                    previous.get("model_calls_total")
                    if "model_calls_total" in previous
                    else None,
                ),
                " 次",
            ),
            "color": "#8a66dd",
        },
        {
            "label": "模型数",
            "value": f"{current['ready_models']} / {current['total_models']}",
            "delta": _delta(
                current["ready_models"],
                previous.get("ready_models") if "ready_models" in previous else None,
            ),
            "delta_text": _format_delta_text(
                _delta(
                    current["ready_models"],
                    previous.get("ready_models")
                    if "ready_models" in previous
                    else None,
                ),
                " 个",
            ),
            "color": "#3557f2",
        },
    ]

    payload = {
        "generated_at": overview["generated_at"],
        "meta": {
            "auto_refresh_seconds": ADMIN_AUTO_REFRESH_SECONDS,
            "compare_label": "较上周",
            "compare_empty_label": "暂无上周",
            "weekly_refresh_rule": "Asia/Shanghai 每周一 08:00",
            "health_score": health_score,
            "health_score_label": "系统健康度",
            "health_score_caption": "整体健康度预估",
        },
        "cards": cards,
        "rings": rings,
        "bars": bars,
        "progress": progress,
        "categories": categories,
        "overview": overview,
    }

    snapshot_metrics = {
        "users": current["users"],
        "completed_tasks": current["completed_tasks"],
        "models_mb": current["models_mb"],
        "exports": current["exports"],
        "active_users_7d": current["active_users_7d"],
        "monthly_power_user_ratio": current["monthly_power_user_ratio"],
        "task_export_rate": current["task_export_rate"],
        "task_success_rate": current["task_success_rate"],
        "system_disk_used_percent": current["system_disk_used_percent"],
        "storage_used_mb": current["storage_used_mb"],
        "training_files": current["training_files"],
        "training_corpus_samples": current["training_corpus_samples"],
        "training_corpus_mb": current["training_corpus_mb"],
        "export_mb": current["export_mb"],
        "assets": current["assets"],
        "model_calls_total": current["model_calls_total"],
        "ready_models": current["ready_models"],
    }
    _write_admin_snapshot(snapshot_metrics, weekly_context)

    return payload


@router.get("/task_logs")
async def admin_task_logs(
    query: str = Query("", description="按用户名、taskId、失败原因搜索"),
    status: str = Query("all", description="all / ok / fail / cancel / info"),
    task_type: str = Query("all", description="all / 模型训练 / 图片追色 / 视频追色 / 导出"),
    limit: int = Query(50, ge=1, le=200),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    runtime_stats = load_runtime_stats()
    disk_stats = _disk_usage_stats(BASE_DIR)
    memory_stats = _memory_usage_stats()
    live_resource_snapshot = _live_resource_snapshot(BASE_DIR)
    active_tasks = progress_manager.active_task_count()
    logs = get_task_logs(runtime_stats, query=query, status=status, task_type=task_type, limit=limit)

    users = (await db.execute(select(User))).scalars().all()
    user_map = {}
    for user in users:
        for key in _user_lookup_keys(user):
            user_map[key] = user

    logs = [
        _standardize_admin_log_entry(entry, user_map, live_resource_snapshot)
        for entry in logs
    ]

    status_counts = {
        "ok": 0,
        "fail": 0,
        "cancel": 0,
        "info": 0,
    }
    failure_rank = {}
    for entry in get_task_logs(runtime_stats, query="", status="all", task_type="all", limit=200):
        entry_status = str(entry.get("status") or "info").lower()
        if entry_status in status_counts:
            status_counts[entry_status] += 1
        if entry_status == "fail":
            key = str(entry.get("summary") or "未知失败").strip() or "未知失败"
            failure_rank[key] = failure_rank.get(key, 0) + 1

    failure_top = [
        {"reason": item[0], "count": item[1]}
        for item in sorted(failure_rank.items(), key=lambda kv: kv[1], reverse=True)[:6]
    ]

    return {
        "logs": logs,
        "alerts": _build_admin_alerts(
            disk_stats,
            memory_stats,
            active_tasks,
            live_resource_snapshot.get("cpu_used_percent"),
        ),
        "resource_summary": live_resource_snapshot,
        "summary": {
            "total": sum(status_counts.values()),
            "success": status_counts["ok"],
            "failed": status_counts["fail"],
            "cancelled": status_counts["cancel"],
            "active_tasks": active_tasks,
        },
        "failure_top": failure_top,
        "resource": {
            "disk": disk_stats,
            "memory": memory_stats,
        },
    }


@router.post("/task_logs/backfill")
async def admin_task_logs_backfill(
    _admin: User = Depends(require_admin),
):
    result = await backfill_admin_task_logs()
    return {
        "success": True,
        "message": f"历史日志身份回填完成，本次更新 {int(result.get('updated_logs') or 0)} 条。",
        "result": result,
    }


def _user_lookup_keys(user: User):
    keys = set()
    if user is None:
        return keys
    keys.add(str(user.id))
    for value in (user.email, user.phone, user.qq_id, user.wechat_id):
        raw = str(value or "").strip().lower()
        if raw:
            keys.add(raw)
    display_name = str(_derive_display_name(user) or "").strip().lower()
    if display_name:
        keys.add(display_name)
    return keys
