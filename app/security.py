import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.settings import IS_PRODUCTION, int_env

DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES = 300 * 1024 * 1024
DEFAULT_VIDEO_UPLOAD_MAX_BYTES = 300 * 1024 * 1024
DEFAULT_UPLOAD_RATE_LIMIT = 30
DEFAULT_AI_RATE_LIMIT = 12
DEFAULT_GLOBAL_AI_CONCURRENCY = 2
DEFAULT_USER_AI_CONCURRENCY = 1
RATE_LIMIT_WINDOW_SECONDS = 60

UPLOAD_LIMIT_PATHS = {
    "/api/upload_batch",
    "/api/train/upload",
    "/api/prepare_lr_preset",
    "/api/apply_profile",
    "/api/apply_style",
    "/api/semantic/match",
    "/api/depth/layers",
    "/api/mask/subject",
    "/api/projects/space_profile/avatar",
    "/api/projects/upload",
    "/api/capture_style",
    "/api/preview_upload",
    "/api/video_metadata",
}
VIDEO_UPLOAD_LIMIT_PATHS = {
    "/api/video_transfer",
    "/api/export_video",
    "/api/transfer",
}
AI_LIMIT_PATHS = {
    "/api/transfer",
    "/api/video_transfer",
    "/api/download_full",
    "/api/export_video",
    "/api/train",
    "/api/depth/layers",
    "/api/semantic/match",
    "/api/mask/subject",
    "/api/merge_luts",
    "/api/apply_profile",
    "/api/apply_style",
}
UPLOAD_RATE_LIMIT_PATHS = UPLOAD_LIMIT_PATHS | VIDEO_UPLOAD_LIMIT_PATHS
IMAGE_ORIGINAL_UPLOAD_LIMIT_PATHS = {
    "/api/upload_batch",
    "/api/train/upload",
    "/api/transfer",
    "/api/video_transfer",
    "/api/export_video",
    "/api/preview_upload",
    "/api/capture_style",
    "/api/video_metadata",
}

RATE_BUCKETS = defaultdict(deque)
USER_AI_COUNTS = defaultdict(int)
LIMIT_LOCK = asyncio.Lock()
GLOBAL_AI_SEMAPHORE = None


@dataclass
class RequestLimitLease:
    response: Optional[JSONResponse] = None
    ai_slot_key: Optional[str] = None
    ai_semaphore: Optional[asyncio.Semaphore] = None
    ai_global_acquired: bool = False

    async def release(self) -> None:
        if self.ai_global_acquired and self.ai_semaphore is not None:
            self.ai_semaphore.release()
        if self.ai_slot_key is not None:
            await _release_user_ai_slot(self.ai_slot_key)


def require_local_admin_tools_enabled():
    if IS_PRODUCTION and os.environ.get("COLORCHASE_ENABLE_LOCAL_ADMIN_TOOLS") != "1":
        raise HTTPException(status_code=404, detail="Not found")


def _is_project_upload_path(path: str) -> bool:
    parts = path.strip("/").split("/")
    return len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "upload"


def _is_upload_limited_path(path: str) -> bool:
    return (
        path in UPLOAD_RATE_LIMIT_PATHS
        or path == "/api/projects/space_profile/avatar"
        or path == "/api/capture_style"
        or _is_project_upload_path(path)
    )


def _is_video_upload_limited_path(path: str) -> bool:
    return path in VIDEO_UPLOAD_LIMIT_PATHS


def _is_image_original_upload_path(path: str) -> bool:
    return path in IMAGE_ORIGINAL_UPLOAD_LIMIT_PATHS


def _is_ai_limited_path(path: str) -> bool:
    return (
        path in AI_LIMIT_PATHS
        or (path.startswith("/api/admin/models/") and path.endswith("/benchmark"))
    )


def _client_key(request: Request, user_id: Optional[int]) -> str:
    if user_id is not None:
        return f"user:{user_id}"
    forwarded = request.headers.get("x-forwarded-for", "")
    host = forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown")
    return f"ip:{host}"


def _content_length(request: Request) -> Optional[int]:
    raw = request.headers.get("content-length")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def ensure_request_content_length(request: Request, max_bytes: int) -> None:
    length = _content_length(request)
    if length is None and IS_PRODUCTION:
        raise HTTPException(status_code=411, detail="Content-Length required")
    if length is not None and length > max_bytes:
        raise HTTPException(status_code=413, detail="Uploaded file is too large")


def get_upload_file_size(file: UploadFile) -> Optional[int]:
    if file is None:
        return None
    size = getattr(file, "size", None)
    if size is not None:
        try:
            return max(int(size), 0)
        except (TypeError, ValueError):
            pass
    file_obj = getattr(file, "file", None)
    if file_obj is None:
        return None
    try:
        current = file_obj.tell()
        file_obj.seek(0, os.SEEK_END)
        size = file_obj.tell()
        file_obj.seek(current)
        return max(int(size), 0)
    except Exception:
        return None


def ensure_upload_file_size(file: UploadFile, max_bytes: int, label: str = "文件") -> None:
    size = get_upload_file_size(file)
    if size is None:
        return
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"{label}过大，单个文件最大 {max_bytes // 1024 // 1024}MB")


async def _check_rate_limit(bucket_name: str, key: str, limit: int) -> None:
    now = time.monotonic()
    bucket_key = (bucket_name, key)
    async with LIMIT_LOCK:
        bucket = RATE_BUCKETS[bucket_key]
        while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests")
        bucket.append(now)


async def _acquire_user_ai_slot(key: str):
    limit = int_env("COLORCHASE_USER_AI_CONCURRENCY", DEFAULT_USER_AI_CONCURRENCY)
    async with LIMIT_LOCK:
        if USER_AI_COUNTS[key] >= limit:
            raise HTTPException(status_code=429, detail="Too many concurrent AI tasks")
        USER_AI_COUNTS[key] += 1
    return key


async def _release_user_ai_slot(key: str) -> None:
    async with LIMIT_LOCK:
        current = USER_AI_COUNTS.get(key, 0)
        if current <= 1:
            USER_AI_COUNTS.pop(key, None)
        else:
            USER_AI_COUNTS[key] = current - 1


def _global_ai_semaphore():
    global GLOBAL_AI_SEMAPHORE
    if GLOBAL_AI_SEMAPHORE is None:
        GLOBAL_AI_SEMAPHORE = asyncio.Semaphore(
            int_env("COLORCHASE_GLOBAL_AI_CONCURRENCY", DEFAULT_GLOBAL_AI_CONCURRENCY)
        )
    return GLOBAL_AI_SEMAPHORE


async def begin_request_limits(request: Request, user_id: Optional[int]) -> RequestLimitLease:
    lease = RequestLimitLease()
    if request.method.upper() not in {"POST", "PUT", "PATCH"}:
        return lease

    client_key = _client_key(request, user_id)
    path = request.url.path

    if _is_upload_limited_path(path):
        if _is_image_original_upload_path(path):
            max_bytes = int_env(
                "COLORCHASE_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES",
                DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES,
            )
        elif _is_video_upload_limited_path(path):
            max_bytes = int_env("COLORCHASE_VIDEO_UPLOAD_MAX_BYTES", DEFAULT_VIDEO_UPLOAD_MAX_BYTES)
        else:
            max_bytes = int_env("COLORCHASE_UPLOAD_MAX_BYTES", DEFAULT_UPLOAD_MAX_BYTES)
        try:
            ensure_request_content_length(request, max_bytes)
        except HTTPException as exc:
            lease.response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return lease
        try:
            await _check_rate_limit(
                "upload",
                client_key,
                int_env("COLORCHASE_UPLOAD_RATE_LIMIT", DEFAULT_UPLOAD_RATE_LIMIT),
            )
        except HTTPException as exc:
            lease.response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return lease

    if _is_ai_limited_path(path):
        try:
            await _check_rate_limit(
                "ai",
                client_key,
                int_env("COLORCHASE_AI_RATE_LIMIT", DEFAULT_AI_RATE_LIMIT),
            )
            lease.ai_slot_key = await _acquire_user_ai_slot(client_key)
        except HTTPException as exc:
            lease.response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return lease

        lease.ai_semaphore = _global_ai_semaphore()
        try:
            await asyncio.wait_for(lease.ai_semaphore.acquire(), timeout=2.0)
            lease.ai_global_acquired = True
        except asyncio.TimeoutError:
            await lease.release()
            lease.ai_slot_key = None
            lease.response = JSONResponse(
                status_code=429,
                content={"detail": "Too many concurrent AI tasks"},
            )

    return lease
