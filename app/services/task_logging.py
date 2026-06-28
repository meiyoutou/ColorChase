import ctypes
import json
import os
import shutil
import sqlite3
import time
from typing import Optional


def create_task_log_writer(base_dir, user_profile_record, record_task_log):
    def _json_safe(value):
        if isinstance(value, dict):
            return {key: _json_safe(item) for key, item in value.items()}
        if isinstance(value, set):
            return sorted(_json_safe(item) for item in value)
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return value

    def _build_user_label(user_id: Optional[int], role: str = "") -> str:
        if user_id is None:
            return "游客"
        suffix = "admin" if role == "admin" else "user"
        return f"user_{user_id}_{suffix}"

    def _load_log_user_snapshot(user_id: Optional[int], role: str = "") -> dict:
        fallback = {
            "id": user_id,
            "display_name": _build_user_label(user_id, role) if user_id is not None else "游客",
            "email": "",
            "role": role or "user",
            "avatar_url": "",
        }
        if user_id is None:
            fallback["role"] = role or ""
            return fallback

        try:
            conn = sqlite3.connect(str(base_dir / "colorchase.db"))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, email, phone, role FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
            conn.close()
        except Exception:
            return fallback

        if not row:
            return fallback

        profile_record = user_profile_record(int(row["id"]))
        nickname = str(profile_record.get("nickname") or "").strip()
        email = str(row["email"] or "").strip()
        phone = str(row["phone"] or "").strip()
        display_name = nickname
        if not display_name and email:
            display_name = email.split("@", 1)[0]
        if not display_name and phone:
            display_name = phone[-4:] if len(phone) >= 4 else phone
        if not display_name:
            display_name = f"用户{int(row['id'])}"

        return {
            "id": int(row["id"]),
            "display_name": display_name,
            "email": email,
            "role": str(row["role"] or role or "user"),
            "avatar_url": str(profile_record.get("avatar_path") or "").strip(),
        }

    def _format_log_meta_value(key: str, value):
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
            source_map = {
                "frontend_export": "前端导出",
                "apply_profile": "配置应用",
                "apply_style": "风格应用",
            }
            return source_map.get(str(value), str(value))
        if isinstance(value, (dict, list, tuple, set)):
            return json.dumps(_json_safe(value), ensure_ascii=False)
        return str(value)

    def _build_log_meta_display(meta_payload: dict) -> list:
        if not isinstance(meta_payload, dict):
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
        display = []
        for key, value in meta_payload.items():
            if value is None or value == "":
                continue
            display.append({
                "label": label_map.get(key, str(key).replace("_", " ").title()),
                "value": _format_log_meta_value(str(key), value),
            })
        return display

    def _build_log_display_payload(summary: str, detail: str, meta_payload: Optional[dict] = None) -> dict:
        meta_payload = meta_payload if isinstance(meta_payload, dict) else {}
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
            source = str(meta_payload.get("source") or "").strip()
            detail_text = detail_map.get(source, str(summary or "").strip())
        return {
            "summary": str(summary or "").strip(),
            "detail": detail_text,
        }

    def _build_resource_snapshot():
        def _disk_percent():
            disk = shutil.disk_usage(base_dir)
            used_disk = disk.total - disk.free
            return round(used_disk / disk.total * 100, 1) if disk.total else 0

        def _memory_percent_windows():
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
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
                return 0
            return round(float(memory_status.dwMemoryLoad or 0), 1)

        def _cpu_percent_windows(sample_seconds=0.08):
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

                def _to_int(file_time):
                    return (file_time.dwHighDateTime << 32) | file_time.dwLowDateTime

                return _to_int(idle_time), _to_int(kernel_time), _to_int(user_time)

            first = _read_times()
            if not first:
                return 0
            time.sleep(sample_seconds)
            second = _read_times()
            if not second:
                return 0
            idle_delta = second[0] - first[0]
            kernel_delta = second[1] - first[1]
            user_delta = second[2] - first[2]
            total_delta = kernel_delta + user_delta
            if total_delta <= 0:
                return 0
            busy_ratio = max(0.0, min(1.0, 1.0 - (idle_delta / total_delta)))
            return round(busy_ratio * 100, 1)

        try:
            import psutil

            memory = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=None)
            return {
                "memory_used_percent": round(float(memory.percent or 0), 1),
                "disk_used_percent": _disk_percent(),
                "cpu_used_percent": round(float(cpu or 0), 1),
            }
        except Exception:
            return {
                "memory_used_percent": _memory_percent_windows() if os.name == "nt" else 0,
                "disk_used_percent": _disk_percent(),
                "cpu_used_percent": _cpu_percent_windows() if os.name == "nt" else 0,
            }

    def _write_task_log(
        *,
        task_id: str = "",
        task_type: str,
        event_type: str = "result",
        status: str = "info",
        summary: str = "",
        detail: str = "",
        user_id: Optional[int] = None,
        role: str = "",
        model: str = "",
        duration_ms: Optional[int] = None,
        meta: Optional[dict] = None,
    ):
        meta_payload = meta if isinstance(meta, dict) else {}
        if user_id is None:
            from config import get_current_runtime_user
            user_id = get_current_runtime_user()
        user_snapshot = _load_log_user_snapshot(user_id, role)
        display_payload = _build_log_display_payload(summary, detail, meta_payload)
        record_task_log({
            "task_id": task_id,
            "task_type": task_type,
            "event_type": event_type,
            "status": status,
            "summary": display_payload["summary"],
            "detail": display_payload["detail"],
            "display": display_payload,
            "user": user_snapshot,
            "user_id": user_snapshot.get("id"),
            "user_label": user_snapshot.get("display_name"),
            "email": user_snapshot.get("email"),
            "role": user_snapshot.get("role"),
            "avatar_url": user_snapshot.get("avatar_url"),
            "model": model,
            "timing": {"duration_ms": duration_ms},
            "duration_ms": duration_ms,
            "meta_raw": meta_payload,
            "meta": meta_payload,
            "meta_display": _build_log_meta_display(meta_payload),
            "resource": _build_resource_snapshot(),
            "schema_version": 2,
        })

    return _write_task_log
