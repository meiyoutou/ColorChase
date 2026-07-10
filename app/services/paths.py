import re
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import cv2
import numpy as np
from fastapi import HTTPException
from sqlalchemy import select

from app.settings import IS_PRODUCTION
from config import (
    BASE_DIR,
    STORAGE_CACHE_DIR,
    STORAGE_DIR,
    STORAGE_LOGS_DIR,
    STORAGE_PROJECT_ASSETS_DIR,
    STORAGE_TEMP_DIR,
    STORAGE_TRAINING_CORPUS_DIR,
    STORAGE_USERS_DIR,
    get_project_assets_dir,
    get_temp_lut_dir,
    get_upload_dir,
    get_user_assets_dir,
    get_user_images_dir,
    get_user_profiles_dir,
    get_user_references_dir,
    get_video_dir,
    iter_known_project_asset_dirs,
    iter_known_style_extracted_dirs,
)
from database import async_session
from models import Project


def _iter_style_extracted_roots():
    yield from iter_known_style_extracted_dirs()


def _resolve_style_dir(style_id: str):
    safe_id = Path(str(style_id or "")).name
    if not safe_id:
        return None
    for root in _iter_style_extracted_roots():
        candidate = root / safe_id
        if candidate.is_dir():
            return candidate
    return None


def _resolve_style_extracted_file(file_path: str):
    safe_parts = [part for part in Path(str(file_path or "")).parts if part not in ("", ".", "..")]
    if not safe_parts:
        return None
    for root in _iter_style_extracted_roots():
        candidate = (root.joinpath(*safe_parts)).resolve()
        if root == candidate or root in candidate.parents:
            if candidate.exists() and candidate.is_file():
                return candidate
    return None


def _runtime_upload_dir() -> Path:
    return get_upload_dir()


def _runtime_video_dir() -> Path:
    return get_video_dir()


def _runtime_temp_lut_dir() -> Path:
    return get_temp_lut_dir()


def _normalize_project_id(project_id: int) -> int:
    try:
        return int(project_id or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_storage_label(storage_label: str) -> str:
    label = str(storage_label or "").strip()
    if (
        not label
        or label in (".", "..")
        or "/" in label
        or "\\" in label
        or not re.match(r"^[A-Za-z0-9@._-]{1,128}$", label)
    ):
        raise ValueError("invalid storage_label")
    return label


def _project_assets_root_for_label(storage_label: str) -> Path:
    return STORAGE_PROJECT_ASSETS_DIR / _normalize_storage_label(storage_label)


def _runtime_user_temp_dir_for_label(storage_label: str) -> Path:
    return STORAGE_TEMP_DIR / "user_uploads" / _normalize_storage_label(storage_label)


def _user_assets_root_for_label(storage_label: str) -> Path:
    return STORAGE_USERS_DIR / _normalize_storage_label(storage_label)


def _training_corpus_dir_for_label(storage_label: str) -> Path:
    return STORAGE_TRAINING_CORPUS_DIR / _normalize_storage_label(storage_label)


async def _ensure_project_access(project_id: int, user_id: Optional[int]) -> int:
    pid = _normalize_project_id(project_id)
    if pid <= 0:
        return pid
    if user_id is None:
        if IS_PRODUCTION:
            raise HTTPException(status_code=401, detail="Authentication required")
        return pid
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.id == pid, Project.owner_id == user_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="项目不存在")
    return pid


def _safe_project_bucket_dir(project_id: int, bucket: str, storage_label: Optional[str] = None) -> Path:
    pid = _normalize_project_id(project_id)
    if pid <= 0:
        raise ValueError("project_id invalid")
    safe_bucket = re.sub(r"[^A-Za-z0-9._-]+", "_", str(bucket or "assets")).strip("._-")
    if not safe_bucket:
        safe_bucket = "assets"
    root = _project_assets_root_for_label(storage_label) if storage_label else get_project_assets_dir()
    project_root = (root / str(pid)).resolve()
    target = (project_root / safe_bucket).resolve()
    if target != project_root and project_root not in target.parents:
        raise ValueError("invalid project bucket")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _project_bucket_file(project_id: int, bucket: str, filename: str, storage_label: Optional[str] = None) -> tuple[Path, str]:
    safe_name = Path(str(filename or "")).name or uuid.uuid4().hex
    bucket_dir = _safe_project_bucket_dir(project_id, bucket, storage_label=storage_label)
    path = bucket_dir / safe_name
    url = f"/api/project_assets/{_normalize_project_id(project_id)}/{bucket}/{safe_name}"
    return path, url


def _save_project_image(project_id: int, bucket: str, filename: str, img: np.ndarray, ext: str = ".jpg", params=None, storage_label: Optional[str] = None) -> tuple[str, str]:
    path, _url = _project_bucket_file(project_id, bucket, filename, storage_label=storage_label)
    path = path.with_suffix(ext if ext.startswith(".") else f".{ext}")
    if params is None:
        params = [cv2.IMWRITE_JPEG_QUALITY, 95] if path.suffix.lower() in (".jpg", ".jpeg") else [cv2.IMWRITE_PNG_COMPRESSION, 3]
    ok, buf = cv2.imencode(path.suffix.lower(), img, params)
    if not ok or buf is None:
        raise RuntimeError("图像保存失败")
    buf.tofile(str(path))
    url = f"/api/project_assets/{_normalize_project_id(project_id)}/{bucket}/{path.name}"
    return str(path), url


def _iter_project_asset_roots(
    project_id: int,
    storage_label: Optional[str] = None,
    scan_legacy_user_dirs: bool = False,
):
    seen = set()

    def append_project_root(root: Path):
        try:
            resolved = (root / str(project_id)).resolve()
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    if storage_label:
        yield from append_project_root(_project_assets_root_for_label(storage_label))

    try:
        project_assets_base = get_project_assets_dir().resolve()
        allowed_label_root = (
            _project_assets_root_for_label(storage_label).resolve()
            if storage_label
            else None
        )
    except Exception:
        project_assets_base = None
        allowed_label_root = None

    for root in [get_project_assets_dir()] + list(iter_known_project_asset_dirs()):
        try:
            resolved_root = root.resolve()
        except Exception:
            continue
        if (
            not scan_legacy_user_dirs
            and project_assets_base is not None
            and resolved_root.parent == project_assets_base
            and resolved_root.name.startswith("user_")
            and resolved_root != allowed_label_root
        ):
            continue
        yield from append_project_root(root)

    if scan_legacy_user_dirs:
        base = get_project_assets_dir()
        try:
            if base.exists():
                for user_dir in base.iterdir():
                    if not user_dir.is_dir() or not user_dir.name.startswith("user_"):
                        continue
                    yield from append_project_root(user_dir)
        except Exception:
            return


def _safe_project_asset_file(
    project_id: int,
    file_path: str,
    storage_label: Optional[str] = None,
    scan_legacy_user_dirs: bool = False,
) -> Path:
    pid = _normalize_project_id(project_id)
    if pid <= 0:
        raise HTTPException(status_code=404, detail="Project not found")
    relative = Path(str(file_path or "").replace("\\", "/"))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HTTPException(status_code=404, detail="File not found")
    for project_root in _iter_project_asset_roots(
        pid,
        storage_label=storage_label,
        scan_legacy_user_dirs=scan_legacy_user_dirs,
    ):
        try:
            target = (project_root / relative).resolve()
        except Exception:
            continue
        if target != project_root and project_root not in target.parents:
            continue
        if target.exists() and target.is_file():
            return target
    raise HTTPException(status_code=404, detail="File not found")


def _user_asset_roots(storage_label: Optional[str] = None):
    if storage_label:
        root = _user_assets_root_for_label(storage_label)
        return {
            "images": root / "images",
            "references": root / "references",
            "profiles": root / "profiles",
        }
    return {
        "images": get_user_images_dir(),
        "references": get_user_references_dir(),
        "profiles": get_user_profiles_dir(),
    }


def _iter_user_asset_roots(asset_group: str, storage_label: Optional[str] = None, user_id: Optional[int] = None):
    group = str(asset_group or "").strip().lower()
    seen = set()
    candidate_maps = []
    if storage_label:
        candidate_maps.append(_user_asset_roots(storage_label=storage_label))
    candidate_maps.append(_user_asset_roots())
    if user_id is not None:
        candidate_maps.append({
            "images": get_user_assets_dir() / f"user_{int(user_id)}" / "images",
            "references": get_user_assets_dir() / f"user_{int(user_id)}" / "references",
            "profiles": get_user_assets_dir() / f"user_{int(user_id)}" / "profiles",
        })
    for roots in candidate_maps:
        root = roots.get(group)
        if root is None:
            continue
        try:
            resolved = root.resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        yield resolved


def _safe_user_asset_file(asset_group: str, file_path: str, storage_label: Optional[str] = None, user_id: Optional[int] = None) -> Path:
    if str(asset_group or "").strip().lower() not in ("images", "references", "profiles"):
        raise HTTPException(status_code=404, detail="File not found")
    relative = Path(str(file_path or "").replace("\\", "/"))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HTTPException(status_code=404, detail="File not found")
    for root_resolved in _iter_user_asset_roots(asset_group, storage_label=storage_label, user_id=user_id):
        target = (root_resolved / relative).resolve()
        if target != root_resolved and root_resolved not in target.parents:
            continue
        if target.exists() and target.is_file():
            return target
    raise HTTPException(status_code=404, detail="File not found")


def _safe_runtime_file(root: Path, file_path: str) -> Path:
    relative = Path(file_path or "")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return candidate


def _resolve_local_file_path(
    value: str,
    request_user_id: Optional[int] = None,
    request_storage_label: Optional[str] = None,
    allow_workspace_path: bool = False,
) -> Optional[Path]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith(("data:", "blob:", "http://", "https://")):
        return None
    parsed = urlparse(raw)
    route_path = (parsed.path or raw).split("?", 1)[0].replace("\\", "/")
    for prefix in ("/api/project_assets/", "/assets/projects/"):
        if route_path.startswith(prefix):
            rest = route_path[len(prefix):].strip("/")
            parts = rest.split("/", 1)
            if len(parts) != 2 or not parts[0].isdigit():
                return None
            try:
                return _safe_project_asset_file(int(parts[0]), parts[1], storage_label=request_storage_label)
            except HTTPException:
                return None
    if route_path.startswith("/api/user_assets/"):
        rest = route_path[len("/api/user_assets/"):].strip("/")
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return None
        try:
            return _safe_user_asset_file(
                parts[0],
                parts[1],
                storage_label=request_storage_label,
                user_id=request_user_id,
            )
        except HTTPException:
            return None
    if route_path.startswith("/api/user_temp/"):
        rest = route_path[len("/api/user_temp/"):].strip("/")
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return None
        try:
            temp_user_id = int(parts[0])
            if request_user_id is None or temp_user_id != int(request_user_id):
                return None
            return _safe_runtime_user_temp_file(
                temp_user_id,
                parts[1],
                storage_label=request_storage_label,
            )
        except (ValueError, HTTPException):
            return None
    route_roots = {
        "/assets/": get_user_assets_dir(),
        "/temp_luts/": _runtime_temp_lut_dir(),
    }
    for prefix, root in route_roots.items():
        if route_path.startswith(prefix):
            relative = route_path[len(prefix):].strip("/")
            if not relative:
                return None
            candidate = (root / Path(relative)).resolve()
            try:
                root_resolved = root.resolve()
            except Exception:
                root_resolved = root
            if candidate == root_resolved or root_resolved in candidate.parents:
                return candidate if candidate.exists() else None
            return None
    if not allow_workspace_path:
        return None
    candidate = Path(raw)
    try:
        base_resolved = BASE_DIR.resolve()
    except Exception:
        base_resolved = BASE_DIR
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve()
        except Exception:
            return None
        if candidate != base_resolved and base_resolved not in candidate.parents:
            return None
        return candidate if candidate.exists() else None
    if any(part == ".." for part in candidate.parts):
        return None
    candidate = (BASE_DIR / candidate).resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        return None
    return candidate if candidate.exists() else None


def _safe_session_dir(session_id: str) -> Path:
    r"""Validate session_id and return safe subdirectory under temp/luts.
    Rejects path traversal attempts (.., /, \) and non-conforming IDs.
    """
    if not re.match(r"^[A-Za-z0-9_-]{8,64}$", session_id):
        raise HTTPException(status_code=400, detail="无效的 session_id 格式")

    safe_dir = _runtime_temp_lut_dir() / session_id
    safe_dir.mkdir(parents=True, exist_ok=True)
    return safe_dir


def _is_admin_request(authorization: Optional[str]) -> bool:
    """根据 JWT token 判断当前请求是否来自管理员账号。"""
    try:
        from app.services.auth_utils import _get_request_user_role
        return _get_request_user_role(authorization) == "admin"
    except Exception:
        return False


def _runtime_user_temp_dir(user_id: Optional[int] = None, storage_label: Optional[str] = None) -> Path:
    """普通用户上传文件的临时目录，处理完成后会被清理，不落盘到 project/user 目录。

    按 user_{邮箱/手机号} 隔离子目录，和持久化目录（project_assets）的命名逻辑保持一致。
    URL 里仍然用 user_id 做权限校验，磁盘目录名用邮箱是为了人眼可读 + 和持久化目录统一。
    """
    # 函数内 import 避免和 config 的循环依赖
    from config import _resolve_user_id, _resolve_user_label

    if storage_label:
        path = _runtime_user_temp_dir_for_label(storage_label)
        path.mkdir(parents=True, exist_ok=True)
        return path

    from config import _resolve_user_id, _resolve_user_label

    resolved = _resolve_user_id(user_id)
    if resolved is not None:
        label = _resolve_user_label(resolved)
    else:
        label = "anonymous"
    path = STORAGE_TEMP_DIR / "user_uploads" / label
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_to_runtime_user_temp(
    content: bytes,
    user_id: Optional[int],
    filename: str,
    storage_label: Optional[str] = None,
) -> Path:
    """把字节写入普通用户临时目录，返回绝对路径。"""
    if not filename:
        filename = f"{uuid.uuid4().hex}.bin"
    target_dir = _runtime_user_temp_dir(user_id, storage_label=storage_label)
    target = target_dir / filename
    target.write_bytes(content)
    return target


def _runtime_user_temp_url(user_id: Optional[int], filename: str) -> str:
    """生成普通用户临时文件的可访问 URL。"""
    return f"/api/user_temp/{user_id or 0}/{filename}"


def _iter_runtime_user_temp_roots(user_id: int, storage_label: Optional[str] = None):
    seen = set()
    candidates = []
    if storage_label:
        candidates.append(_runtime_user_temp_dir_for_label(storage_label))
    candidates.append(STORAGE_TEMP_DIR / "user_uploads" / f"user_{int(user_id)}")
    candidates.append(_runtime_user_temp_dir(user_id))
    for root in candidates:
        try:
            resolved = root.resolve()
        except Exception:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        yield resolved


def _safe_runtime_user_temp_file(user_id: int, file_path: str, storage_label: Optional[str] = None) -> Path:
    """校验并返回普通用户临时目录下的安全文件路径。"""
    relative = Path(str(file_path or "").replace("\\", "/"))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HTTPException(status_code=404, detail="File not found")
    for root in _iter_runtime_user_temp_roots(user_id, storage_label=storage_label):
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            continue
        if target.exists() and target.is_file():
            return target
    raise HTTPException(status_code=404, detail="File not found")


def cleanup_runtime_user_temp(max_age_seconds: float = 24 * 3600):
    """清理普通用户临时目录下过期的文件，默认保留 24 小时。"""
    import time
    base = STORAGE_TEMP_DIR / "user_uploads"
    if not base.exists():
        return 0
    now = time.time()
    deleted = 0
    for user_dir in base.iterdir():
        if not user_dir.is_dir():
            continue
        for path in user_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    path.unlink()
                    deleted += 1
            except Exception:
                pass
        # 如果目录已空，尝试删除
        try:
            if user_dir.exists() and not any(user_dir.iterdir()):
                user_dir.rmdir()
        except Exception:
            pass
    return deleted


def cleanup_temp_luts(max_age_seconds: float = 24 * 3600):
    """清理 storage/temp/luts/ 下过期的中间文件，默认保留 24 小时。

    清理范围：LUT 矩阵(.npy)、预览图(_preview.jpg)、会话子目录、masks/、depth/、profile_*.xmp。
    这些都是调色处理的中间态，24h 后清理不影响最终交付物（管理员项目资产/普通用户 user_uploads）。
    """
    import time
    base = STORAGE_TEMP_DIR / "luts"
    if not base.exists():
        return 0
    now = time.time()
    deleted = 0
    # 扫描所有文件，删除过期的
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                deleted += 1
        except Exception:
            pass
    # 清理空目录（自底向上，保留 base 本身）
    for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.is_dir() or path == base:
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
        except Exception:
            pass
    return deleted


def cleanup_misc_temp(max_age_seconds: float = 24 * 3600):
    """清理其他临时目录下过期的文件，默认保留 24 小时。

    清理范围：
    - storage/cache/raw_cache/（RAW 解码缓存，可重建）
    - storage/detection/unknown/（未登录用户检测数据，无长期价值）
    - storage/logs/debug_output/（调色调试截图，不需要长期保留）
    """
    import time
    targets = [
        STORAGE_CACHE_DIR / "raw_cache",
        STORAGE_DIR / "detection" / "unknown",
        STORAGE_LOGS_DIR / "debug_output",
    ]
    now = time.time()
    deleted = 0
    for base in targets:
        if not base.exists():
            continue
        # 扫描所有文件，删除过期的
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    path.unlink()
                    deleted += 1
            except Exception:
                pass
        # 清理空目录（自底向上，保留 base 本身）
        for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir() or path == base:
                continue
            try:
                if not any(path.iterdir()):
                    path.rmdir()
            except Exception:
                pass
    return deleted
