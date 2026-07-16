from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.settings import IS_PRODUCTION
from app.services.auth_utils import _resolve_runtime_user_id_from_request
from app.services.paths import _safe_project_asset_file, _safe_runtime_file, _safe_runtime_user_temp_file
from app.services.user_identity import resolve_user_storage_label
from database import async_session
from models import Project


def create_files_router(
    ensure_project_access,
    runtime_temp_lut_dir,
):
    router = APIRouter()

    @router.get("/temp_luts/{file_path:path}")
    async def serve_temp_lut_preview(request: Request, file_path: str):
        request_user_id = _resolve_runtime_user_id_from_request(request)
        if request_user_id is None and IS_PRODUCTION:
            raise HTTPException(status_code=401, detail="Authentication required")
        storage_label = await resolve_user_storage_label(request_user_id) if request_user_id else None
        target = _safe_runtime_file(runtime_temp_lut_dir(storage_label), file_path)
        is_preview = (
            target.suffix.lower() in (".jpg", ".jpeg")
            and (
                # 新格式：result_preview_{session_id}.jpg / orig_preview_{session_id}.jpg
                target.name.startswith(("result_preview_", "orig_preview_"))
                # 旧格式兼容：{session_id}_result_preview.jpg / {session_id}_orig_preview.jpg
                or target.name.endswith(("_result_preview.jpg", "_orig_preview.jpg"))
            )
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
        request: Request,
        project_id: int,
        file_path: str,
    ):
        await ensure_project_access(project_id, _resolve_runtime_user_id_from_request(request))
        async with async_session() as session:
            result = await session.execute(select(Project.owner_id).where(Project.id == project_id))
            owner_id = result.scalar_one_or_none()
        storage_label = await resolve_user_storage_label(owner_id) if owner_id else None
        target = _safe_project_asset_file(
            project_id,
            file_path,
            storage_label=storage_label,
            scan_legacy_user_dirs=True,
        )
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

    @router.get("/api/user_temp/{user_id}/{file_path:path}")
    async def serve_user_temp_asset(
        request: Request,
        user_id: int,
        file_path: str,
    ):
        """普通用户临时上传文件的访问端点，文件在清理后不可再访问。"""
        request_user_id = _resolve_runtime_user_id_from_request(request)
        if request_user_id is None and IS_PRODUCTION:
            raise HTTPException(status_code=401, detail="Authentication required")
        if request_user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问其他用户的临时文件")
        storage_label = await resolve_user_storage_label(user_id)
        target = _safe_runtime_user_temp_file(user_id, file_path, storage_label=storage_label)
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
        }.get(suffix, "application/octet-stream")
        return FileResponse(target, media_type=media_type)

    return router
