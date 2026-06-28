import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

from app.settings import IS_PRODUCTION
from auth import ALGORITHM, SECRET_KEY
from app.services.paths import _safe_project_asset_file, _safe_runtime_file
from config import (
    iter_known_video_dirs,
)
from jose import JWTError, jwt


def _get_request_user_id(authorization: Optional[str]) -> Optional[int]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        return None


def create_files_router(
    ensure_project_access,
    runtime_video_dir,
    runtime_temp_lut_dir,
):
    router = APIRouter()

    @router.get("/videos/{file_path:path}")
    async def serve_video_file(file_path: str):
        if IS_PRODUCTION and os.environ.get("COLORCHASE_ENABLE_PUBLIC_VIDEOS") != "1":
            raise HTTPException(status_code=404, detail="File not found")
        candidate_roots = [runtime_video_dir(), *list(iter_known_video_dirs())]
        tried = set()
        for root in candidate_roots:
            key = str(root)
            if key in tried:
                continue
            tried.add(key)
            target = _safe_runtime_file(root, file_path)
            if target.exists() and target.is_file():
                return FileResponse(target)
        raise HTTPException(status_code=404, detail="File not found")

    @router.get("/temp_luts/{file_path:path}")
    async def serve_temp_lut_preview(file_path: str):
        target = _safe_runtime_file(runtime_temp_lut_dir(), file_path)
        is_preview = (
            target.suffix.lower() in (".jpg", ".jpeg")
            and target.name.endswith(("_result_preview.jpg", "_orig_preview.jpg"))
        )
        is_mask = (
            target.suffix.lower() == ".png"
            and target.parent.name == "masks"
            and target.name.endswith("_mask.png")
        )
        is_depth = (
            target.suffix.lower() == ".png"
            and target.parent.name == "depth"
            and target.name.endswith("_depth.png")
        )
        if (
            not target.exists()
            or not target.is_file()
            or not (is_preview or is_mask or is_depth)
        ):
            raise HTTPException(status_code=404, detail="File not found")
        media_type = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(target, media_type=media_type)

    @router.get("/api/project_assets/{project_id}/{file_path:path}")
    async def serve_project_asset(
        project_id: int,
        file_path: str,
        authorization: Optional[str] = Header(None),
    ):
        await ensure_project_access(project_id, _get_request_user_id(authorization))
        target = _safe_project_asset_file(project_id, file_path)
        suffix = target.suffix.lower()
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".json": "application/json",
        }.get(suffix, "application/octet-stream")
        return FileResponse(target, media_type=media_type)

    return router
