import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from config import CACHE_DIR, LEGACY_CACHE_DIR

STATS_PATH = CACHE_DIR / "admin_runtime_metrics.json"
LEGACY_STATS_PATH = LEGACY_CACHE_DIR / "admin_runtime_metrics.json"
SH_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_LOCK = threading.Lock()

DEFAULT_STATS = {
    "model_calls": {
        "neural_preset": 0,
        "modflows_b0": 0,
        "modflows_b6": 0,
        "total": 0,
    },
    "tasks": {
        "success": 0,
        "failed": 0,
    },
    "exports": {
        "count": 0,
    },
    "daily": {},
    "user_usage": {},
    "task_logs": [],
}
MAX_TASK_LOGS = 600
TASK_LOG_SCHEMA_VERSION = 2


def _normalize_task_log_user(raw_user, fallback_entry=None):
    fallback_entry = fallback_entry if isinstance(fallback_entry, dict) else {}
    raw_user = raw_user if isinstance(raw_user, dict) else {}
    user_id = raw_user.get("id", fallback_entry.get("user_id"))
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    return {
        "id": user_id,
        "display_name": str(
            raw_user.get("display_name")
            or raw_user.get("user_label")
            or fallback_entry.get("user_label")
            or "游客"
        ),
        "email": str(raw_user.get("email") or fallback_entry.get("email") or ""),
        "role": str(raw_user.get("role") or fallback_entry.get("role") or ""),
        "avatar_url": str(raw_user.get("avatar_url") or fallback_entry.get("avatar_url") or ""),
    }


def _normalize_task_log_timing(raw_timing, fallback_entry=None):
    fallback_entry = fallback_entry if isinstance(fallback_entry, dict) else {}
    raw_timing = raw_timing if isinstance(raw_timing, dict) else {}
    return {
        "duration_ms": raw_timing.get("duration_ms", fallback_entry.get("duration_ms")),
    }


def _normalize_task_log_display(raw_display, fallback_entry=None):
    fallback_entry = fallback_entry if isinstance(fallback_entry, dict) else {}
    raw_display = raw_display if isinstance(raw_display, dict) else {}
    return {
        "summary": str(raw_display.get("summary") or fallback_entry.get("summary") or ""),
        "detail": str(raw_display.get("detail") or fallback_entry.get("detail") or ""),
    }


def _normalize_task_log_meta_display(raw_meta_display):
    if not isinstance(raw_meta_display, list):
        return []
    normalized = []
    for item in raw_meta_display:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not label:
            continue
        normalized.append({"label": label, "value": value or "--"})
    return normalized


def _today_key():
    return datetime.now(SH_TZ).strftime("%Y-%m-%d")


def _merge_defaults(stats):
    merged = deepcopy(DEFAULT_STATS)
    if not isinstance(stats, dict):
        return merged
    for key, value in stats.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def load_runtime_stats():
    if STATS_PATH.exists():
        try:
            with open(STATS_PATH, "r", encoding="utf-8") as f:
                return _merge_defaults(json.load(f))
        except Exception:
            return deepcopy(DEFAULT_STATS)
    if LEGACY_STATS_PATH.exists():
        try:
            with open(LEGACY_STATS_PATH, "r", encoding="utf-8") as f:
                return _merge_defaults(json.load(f))
        except Exception:
            return deepcopy(DEFAULT_STATS)
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            return _merge_defaults(json.load(f))
    except Exception:
        return deepcopy(DEFAULT_STATS)


def save_runtime_stats(stats):
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def _ensure_daily(stats, day_key):
    daily = stats.setdefault("daily", {})
    if day_key not in daily or not isinstance(daily[day_key], dict):
        daily[day_key] = {
            "task_success": 0,
            "task_failed": 0,
            "exports": 0,
            "model_calls": 0,
        }
    return daily[day_key]


def _normalize_user_usage(stats):
    user_usage = stats.setdefault("user_usage", {})
    if not isinstance(user_usage, dict):
        stats["user_usage"] = {}
        return stats["user_usage"]
    return user_usage


def _normalize_task_logs(stats):
    task_logs = stats.setdefault("task_logs", [])
    if not isinstance(task_logs, list):
        stats["task_logs"] = []
        return stats["task_logs"]
    return task_logs


def record_task_log(entry):
    if not isinstance(entry, dict):
        return

    created_at = entry.get("created_at") or datetime.now(SH_TZ).isoformat()
    user = _normalize_task_log_user(entry.get("user"), entry)
    timing = _normalize_task_log_timing(entry.get("timing"), entry)
    display = _normalize_task_log_display(entry.get("display"), entry)
    resource = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
    meta_raw = entry.get("meta_raw")
    if not isinstance(meta_raw, dict):
        meta_raw = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    normalized = {
        "id": entry.get("id") or uuid.uuid4().hex[:12],
        "created_at": created_at,
        "task_id": str(entry.get("task_id") or ""),
        "task_type": str(entry.get("task_type") or "未知任务"),
        "event_type": str(entry.get("event_type") or "result"),
        "status": str(entry.get("status") or "info"),
        "model": str(entry.get("model") or ""),
        "summary": str(entry.get("summary") or ""),
        "detail": str(entry.get("detail") or ""),
        "user_id": entry.get("user_id"),
        "user_label": str(entry.get("user_label") or "未知用户"),
        "role": str(entry.get("role") or ""),
        "duration_ms": entry.get("duration_ms"),
        "meta": entry.get("meta") if isinstance(entry.get("meta"), dict) else {},
        "resource": entry.get("resource") if isinstance(entry.get("resource"), dict) else {},
        "summary": display["summary"],
        "detail": display["detail"],
        "display": display,
        "user": user,
        "user_id": user.get("id"),
        "user_label": user.get("display_name"),
        "email": user.get("email"),
        "role": user.get("role"),
        "avatar_url": user.get("avatar_url"),
        "timing": timing,
        "duration_ms": timing.get("duration_ms"),
        "meta_raw": meta_raw,
        "meta": meta_raw,
        "meta_display": _normalize_task_log_meta_display(entry.get("meta_display")),
        "resource": resource,
        "schema_version": int(entry.get("schema_version") or TASK_LOG_SCHEMA_VERSION),
    }

    with _LOCK:
        stats = load_runtime_stats()
        task_logs = _normalize_task_logs(stats)
        task_logs.append(normalized)
        stats["task_logs"] = task_logs[-MAX_TASK_LOGS:]
        save_runtime_stats(stats)


def get_task_logs(stats, query="", status="all", task_type="all", limit=50):
    task_logs = list(_normalize_task_logs(stats))
    normalized_query = str(query or "").strip().lower()
    normalized_status = str(status or "all").strip().lower()
    normalized_task_type = str(task_type or "all").strip().lower()

    filtered = []
    for entry in reversed(task_logs):
        if normalized_status not in ("", "all") and str(entry.get("status") or "").lower() != normalized_status:
            continue
        if normalized_task_type not in ("", "all") and str(entry.get("task_type") or "").lower() != normalized_task_type:
            continue
        if normalized_query:
            haystack = " ".join(
                [
                    str(entry.get("user_label") or ""),
                    str(entry.get("task_id") or ""),
                    str(entry.get("task_type") or ""),
                    str(entry.get("model") or ""),
                    str(entry.get("summary") or ""),
                    str(entry.get("detail") or ""),
                ]
            ).lower()
            if normalized_query not in haystack:
                continue
        filtered.append(entry)
        if len(filtered) >= max(int(limit or 0), 1):
            break
    return filtered


def record_task_outcome(success):
    with _LOCK:
        stats = load_runtime_stats()
        bucket = stats.setdefault("tasks", {})
        day = _ensure_daily(stats, _today_key())
        if success:
            bucket["success"] = int(bucket.get("success", 0)) + 1
            day["task_success"] = int(day.get("task_success", 0)) + 1
        else:
            bucket["failed"] = int(bucket.get("failed", 0)) + 1
            day["task_failed"] = int(day.get("task_failed", 0)) + 1
        save_runtime_stats(stats)


def record_export(count=1):
    count = max(int(count or 0), 0)
    if count <= 0:
        return
    with _LOCK:
        stats = load_runtime_stats()
        bucket = stats.setdefault("exports", {})
        day = _ensure_daily(stats, _today_key())
        bucket["count"] = int(bucket.get("count", 0)) + count
        day["exports"] = int(day.get("exports", 0)) + count
        save_runtime_stats(stats)


def record_model_call(model_key):
    if model_key not in ("neural_preset", "modflows_b0", "modflows_b6"):
        return
    with _LOCK:
        stats = load_runtime_stats()
        bucket = stats.setdefault("model_calls", {})
        day = _ensure_daily(stats, _today_key())
        bucket[model_key] = int(bucket.get(model_key, 0)) + 1
        bucket["total"] = int(bucket.get("total", 0)) + 1
        day["model_calls"] = int(day.get("model_calls", 0)) + 1
        save_runtime_stats(stats)


def record_user_usage(user_id, count=1, at=None):
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return

    usage_count = max(int(count or 0), 0)
    if usage_count <= 0:
        return

    recorded_at = at or datetime.now(SH_TZ)
    day_key = recorded_at.strftime("%Y-%m-%d")

    with _LOCK:
        stats = load_runtime_stats()
        user_usage = _normalize_user_usage(stats)
        user_bucket = user_usage.setdefault(str(normalized_user_id), {})
        if not isinstance(user_bucket, dict):
            user_bucket = {}
            user_usage[str(normalized_user_id)] = user_bucket
        user_bucket[day_key] = int(user_bucket.get(day_key, 0)) + usage_count
        save_runtime_stats(stats)


def get_monthly_user_usage(stats, now=None):
    current_time = now or datetime.now(SH_TZ)
    month_prefix = current_time.strftime("%Y-%m")
    usage_by_user = {}

    for user_id, daily_usage in _normalize_user_usage(stats).items():
        if not isinstance(daily_usage, dict):
            continue
        total = 0
        for day_key, count in daily_usage.items():
            if not str(day_key).startswith(month_prefix):
                continue
            try:
                total += int(count or 0)
            except (TypeError, ValueError):
                continue
        usage_by_user[user_id] = total

    return usage_by_user
