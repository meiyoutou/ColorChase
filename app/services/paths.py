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


def _safe_project_bucket_dir(project_id: int, bucket: str) -> Path:
    pid = _normalize_project_id(project_id)
    if pid <= 0:
        raise ValueError("project_id invalid")
    safe_bucket = re.sub(r"[^A-Za-z0-9._-]+", "_", str(bucket or "assets")).strip("._-")
    if not safe_bucket:
        safe_bucket = "assets"
    project_root = (get_project_assets_dir() / str(pid)).resolve()
    target = (project_root / safe_bucket).resolve()
    if target != project_root and project_root not in target.parents:
        raise ValueError("invalid project bucket")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _project_bucket_file(project_id: int, bucket: str, filename: str) -> tuple[Path, str]:
    safe_name = Path(str(filename or "")).name or uuid.uuid4().hex
    bucket_dir = _safe_project_bucket_dir(project_id, bucket)
    path = bucket_dir / safe_name
    url = f"/api/project_assets/{_normalize_project_id(project_id)}/{bucket}/{safe_name}"
    return path, url


def _save_project_image(project_id: int, bucket: str, filename: str, img: np.ndarray, ext: str = ".jpg", params=None) -> tuple[str, str]:
    path, _url = _project_bucket_file(project_id, bucket, filename)
    path = path.with_suffix(ext if ext.startswith(".") else f".{ext}")
    if params is None:
        params = [cv2.IMWRITE_JPEG_QUALITY, 95] if path.suffix.lower() in (".jpg", ".jpeg") else [cv2.IMWRITE_PNG_COMPRESSION, 3]
    ok, buf = cv2.imencode(path.suffix.lower(), img, params)
    if not ok or buf is None:
        raise RuntimeError("图像保存失败")
    buf.tofile(str(path))
    url = f"/api/project_assets/{_normalize_project_id(project_id)}/{bucket}/{path.name}"
    return str(path), url


def _safe_project_asset_file(project_id: int, file_path: str) -> Path:
    pid = _normalize_project_id(project_id)
    if pid <= 0:
        raise HTTPException(status_code=404, detail="Project not found")
    relative = Path(str(file_path or "").replace("\\", "/"))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HTTPException(status_code=404, detail="File not found")
    candidate_roots = [get_project_assets_dir()] + list(iter_known_project_asset_dirs())
    seen = set()
    for root in candidate_roots:
        try:
            project_root = (root / str(pid)).resolve()
            target = (project_root / relative).resolve()
        except Exception:
            continue
        key = str(project_root)
        if key in seen:
            continue
        seen.add(key)
        if target != project_root and project_root not in target.parents:
            continue
        if target.exists() and target.is_file():
            return target
    raise HTTPException(status_code=404, detail="File not found")


def _user_asset_roots():
    return {
        "images": get_user_images_dir(),
        "references": get_user_references_dir(),
        "profiles": get_user_profiles_dir(),
    }


def _safe_user_asset_file(asset_group: str, file_path: str) -> Path:
    root = _user_asset_roots().get(str(asset_group or "").strip().lower())
    if root is None:
        raise HTTPException(status_code=404, detail="File not found")
    relative = Path(str(file_path or "").replace("\\", "/"))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HTTPException(status_code=404, detail="File not found")
    root_resolved = root.resolve()
    target = (root_resolved / relative).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _safe_runtime_file(root: Path, file_path: str) -> Path:
    relative = Path(file_path or "")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return candidate


def _resolve_local_file_path(value: str) -> Optional[Path]:
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
                return _safe_project_asset_file(int(parts[0]), parts[1])
            except HTTPException:
                return None
    if route_path.startswith("/api/user_assets/"):
        rest = route_path[len("/api/user_assets/"):].strip("/")
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return None
        try:
            return _safe_user_asset_file(parts[0], parts[1])
        except HTTPException:
            return None
    route_roots = {
        "/assets/": get_user_assets_dir(),
        "/videos/": _runtime_video_dir(),
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
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = BASE_DIR / raw
    try:
        resolved = candidate.resolve()
    except Exception:
        return None
    return resolved if resolved.exists() else None
