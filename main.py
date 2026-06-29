import os
import json
import hashlib
import uuid
import time
import base64
import asyncio
import tempfile
import traceback
import shutil
import ctypes
import sqlite3
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, Response
from jose import JWTError, jwt
from sqlalchemy import select

from algorithms.color_transfer import transfer_color
from algorithms.metrics import evaluate_transfer
from algorithms.depth_layers import (
    apply_depth_layer_blend,
    build_depth_cache_key,
    generate_depth_map,
    get_depth_anything_status,
    load_depth_file,
    save_depth_png,
)
from algorithms.semantic_match import (
    build_semantic_cache_key,
    get_dinov2_status,
    semantic_match_transfer,
    summarize_semantic_matches,
)
from algorithms.subject_mask import (
    apply_subject_mask_blend,
    build_mask_cache_key,
    generate_subject_mask,
    get_birefnet_status,
    get_subject_mask_status,
    load_mask_file,
    save_mask_png,
)
from core.color.lut_ops import (
    apply_pro_adjust,
    _build_identity_lut,
    _generate_builtin_profile,
    _generate_orange_bw_lut,
    _trilinear_lookup,
)
from core.io.lut_session import _load_lut_for_session, _save_lut_as_style_preset
from admin_runtime_metrics import record_export, record_model_call, record_task_log, record_task_outcome, record_user_usage
from auth import ALGORITHM, AUTH_COOKIE_NAME, SECRET_KEY
from app.routes.projects import _derive_display_name, _read_project_snapshot, _user_profile_record, run_startup_legacy_asset_migration
from progress import progress_manager
from core.io.loaders import load_image_bgr
from database import async_session
from models import User, Project
from app.settings import ENVIRONMENT, IS_PRODUCTION, allowed_hosts, allowed_origins
from app.security import begin_request_limits, require_local_admin_tools_enabled, ensure_upload_file_size
from app.services.training_corpus import (
    TRAINING_IMAGE_EXTENSIONS,
    _archive_training_sample,
    _ensure_training_target_sample,
    get_training_data_stats_payload as _get_training_data_stats_payload,
    resolve_training_dir as _resolve_training_dir,
    run_startup_training_corpus_backfill,
)
from app.routes.meta import create_meta_router
from app.routes.analysis import create_analysis_router
from app.routes.files import create_files_router
from app.routes.lut import router as lut_router
from app.routes.model_status import create_model_status_router
from app.routes.progress import create_progress_router
from app.routes.styles import router as styles_router
from app.services.auth_utils import (
    _decode_request_payload,
    _extract_request_token,
    _get_request_user_id,
    _get_request_user_role,
    _resolve_runtime_user_id_from_request,
    _task_elapsed_ms,
)
from app.services.model_management import (
    _disabled_model_error,
    _force_model_ready,
    _load_model_management_runtime,
    _merge_public_model_management,
    _model_key_to_algorithm,
    _resolve_depth_model_choice,
    _resolve_mask_model_choice,
    _resolve_semantic_model_choice,
    _resolve_transfer_model_runtime,
)
from app.services.paths import (
    _ensure_project_access,
    _normalize_project_id,
    _project_bucket_file,
    _resolve_local_file_path,
    _resolve_style_extracted_file,
    _runtime_temp_lut_dir,
    _runtime_upload_dir,
    _runtime_video_dir,
    _safe_project_asset_file,
    _safe_project_bucket_dir,
    _safe_runtime_file,
    _safe_user_asset_file,
    _save_project_image,
    _user_asset_roots,
)
from app.services.task_logging import create_task_log_writer
from app.routes.training import create_training_router
from app.routes.task import create_task_router

USER_SPACE_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@lru_cache(maxsize=1)
def _load_neural_preset_transfer():
    from algorithms.neural_preset import neural_preset_transfer as _impl
    return _impl


@lru_cache(maxsize=1)
def _load_modflows_transfer():
    from algorithms.modflows import modflows_transfer as _impl
    return _impl


@lru_cache(maxsize=1)
def _load_postprocess_exports():
    from algorithms.postprocess import (
        enhance_transfer_result as _enhance_transfer_result,
        regional_transfer as _regional_transfer,
    )
    return _enhance_transfer_result, _regional_transfer


def neural_preset_transfer(*args, **kwargs):
    return _load_neural_preset_transfer()(*args, **kwargs)


def modflows_transfer(*args, **kwargs):
    return _load_modflows_transfer()(*args, **kwargs)


def enhance_transfer_result(*args, **kwargs):
    _enhance_transfer_result, _regional_transfer = _load_postprocess_exports()
    return _enhance_transfer_result(*args, **kwargs)


def regional_transfer(*args, **kwargs):
    _enhance_transfer_result, _regional_transfer = _load_postprocess_exports()
    return _regional_transfer(*args, **kwargs)


def _load_local_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            if not key:
                continue
            os.environ.setdefault(key, value.strip())
    except Exception as exc:
        print(f"[ENV] load .env failed: {exc}")


_load_local_env()

app = FastAPI(
    title="ColorChase - AI智能追色工具",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    print(f"[500 ERROR] {request.url} -> {type(exc).__name__}: {exc}")
    print(tb)
    if IS_PRODUCTION:
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=allowed_hosts(),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from config import (
    MODEL_DIR,
    BASE_DIR,
    DEFAULT_PATHS,
    MODFLOWS_B0_CHECKPOINT,
    MODFLOWS_B6_CHECKPOINT,
    NEURALPRESET_MODEL_DIR,
    NEURALPRESET_NORM_WEIGHTS,
    NEURALPRESET_STYLE_WEIGHTS,
    STORAGE_STYLES_EXTRACTED_DIR,
    STATIC_DIR,
    ensure_runtime_dirs,
    get_neuralpreset_weight_status,
    get_current_runtime_path_strings,
    iter_known_video_dirs,
    get_upload_dir,
    get_video_dir,
    get_temp_lut_dir,
    get_project_assets_dir,
    iter_known_project_asset_dirs,
    iter_known_style_dirs,
    iter_known_style_extracted_dirs,
    get_user_assets_dir,
    get_user_images_dir,
    get_user_profiles_dir,
    get_user_references_dir,
    reset_current_runtime_user,
    set_current_runtime_user,
)
ensure_runtime_dirs()
os.makedirs(str(MODEL_DIR), exist_ok=True)

STYLES_EXTRACTED_DIR = STORAGE_STYLES_EXTRACTED_DIR
os.makedirs(str(STYLES_EXTRACTED_DIR), exist_ok=True)
STYLES_DIR = BASE_DIR / "styles"
os.makedirs(str(STYLES_DIR), exist_ok=True)

USER_ASSETS_DIR = get_user_assets_dir()
USER_IMAGES_DIR = get_user_images_dir()
USER_REFS_DIR = get_user_references_dir()
USER_PROFILES_DIR = get_user_profiles_dir()
os.makedirs(str(USER_IMAGES_DIR), exist_ok=True)
os.makedirs(str(USER_REFS_DIR), exist_ok=True)
os.makedirs(str(USER_PROFILES_DIR), exist_ok=True)
os.makedirs(str(get_project_assets_dir()), exist_ok=True)
_write_task_log = create_task_log_writer(BASE_DIR, _user_profile_record, record_task_log)


@app.get("/styles/extracted/{file_path:path}")
async def serve_style_extracted_file(file_path: str):
    target = _resolve_style_extracted_file(file_path)
    if not target:
        raise HTTPException(status_code=404, detail="Style asset not found")
    return FileResponse(str(target))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
if not IS_PRODUCTION:
    app.mount("/assets", StaticFiles(directory=str(USER_ASSETS_DIR)), name="assets")
app.mount("/styles", StaticFiles(directory=str(STYLES_DIR)), name="styles")


@app.get("/__legacy/videos/{file_path:path}")
async def serve_video_file(file_path: str):
    if IS_PRODUCTION and os.environ.get("COLORCHASE_ENABLE_PUBLIC_VIDEOS") != "1":
        raise HTTPException(status_code=404, detail="File not found")
    candidate_roots = [_runtime_video_dir(), *list(iter_known_video_dirs())]
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


@app.get("/__legacy/temp_luts/{file_path:path}")
async def serve_temp_lut_preview(file_path: str):
    target = _safe_runtime_file(_runtime_temp_lut_dir(), file_path)
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


@app.get("/__legacy/api/project_assets/{project_id}/{file_path:path}")
async def serve_project_asset(
    project_id: int,
    file_path: str,
    authorization: Optional[str] = Header(None),
):
    await _ensure_project_access(project_id, _get_request_user_id(authorization))
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


@app.get("/api/user_assets/{asset_group}/{file_path:path}")
async def serve_user_asset(
    asset_group: str,
    file_path: str,
    request: Request,
):
    request_user_id = _resolve_runtime_user_id_from_request(request)
    if request_user_id is None and IS_PRODUCTION:
        raise HTTPException(status_code=401, detail="Authentication required")
    target = _safe_user_asset_file(asset_group, file_path)
    suffix = target.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".json": "application/json",
    }.get(suffix, "application/octet-stream")
    return FileResponse(target, media_type=media_type)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    request_user_id = _resolve_runtime_user_id_from_request(request)
    runtime_user_token = set_current_runtime_user(request_user_id)
    limit_lease = None
    try:
        limit_lease = await begin_request_limits(request, request_user_id)
        if limit_lease.response is not None:
            return limit_lease.response
        response = await call_next(request)
        if request.url.path.startswith(("/static/", "/assets/", "/videos/", "/api/project_assets/", "/api/user_assets/")):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response
    finally:
        if limit_lease is not None:
            await limit_lease.release()
        reset_current_runtime_user(runtime_user_token)

from app.routes.style_capture import router as style_capture_router
app.include_router(style_capture_router)

from database import init_db
from app.routes.auth import router as auth_router
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])

from app.routes.projects import router as projects_router
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])

from app.routes.admin import router as admin_router
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
from app.routes.admin_models import router as admin_models_router
app.include_router(admin_models_router, prefix="/api/admin", tags=["admin-models"])

from app.routes.portal import router as portal_router
app.include_router(portal_router, prefix="/api", tags=["portal"])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    migration_stats = await run_startup_legacy_asset_migration()
    if migration_stats.get("updated_profiles") or migration_stats.get("updated_project_snapshots"):
        print(
            "[Legacy Assets] migrated profiles={profiles} project_snapshots={snapshots}".format(
                profiles=migration_stats.get("updated_profiles", 0),
                snapshots=migration_stats.get("updated_project_snapshots", 0),
            )
        )
    _app.state.training_corpus_backfill_task = asyncio.create_task(
        run_startup_training_corpus_backfill(_resolve_local_file_path)
    )
    yield
    backfill_task = getattr(_app.state, "training_corpus_backfill_task", None)
    if backfill_task is not None and not backfill_task.done():
        backfill_task.cancel()
        try:
            await backfill_task
        except asyncio.CancelledError:
            pass


app.router.lifespan_context = lifespan


def _save_upload(file: UploadFile, project_id: int = 0, bucket: str = "uploads") -> str:
    ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    if _normalize_project_id(project_id) > 0:
        filepath = _safe_project_bucket_dir(project_id, bucket) / filename
    else:
        filepath = os.path.join(str(_runtime_upload_dir()), filename)
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"上传文件为空: {file.filename or '未命名文件'}")
    with open(filepath, "wb") as f:
        f.write(content)
    return str(filepath)


PREVIEW_MAX_SIZE = 1024


def _cv2_imread_full(filepath):
    arr = np.fromfile(filepath, dtype=np.uint8)
    if arr.size == 0:
        return None
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _cv2_imread(filepath: str, target_size: int = None, mode: str = "preview") -> np.ndarray:
    if target_size is None and mode == "preview":
        target_size = PREVIEW_MAX_SIZE
    try:
        bgr, meta = load_image_bgr(filepath, target_size=target_size, mode=mode)
        return bgr
    except Exception:
        arr = np.fromfile(filepath, dtype=np.uint8)
        if arr.size == 0:
            return None
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None and target_size and max(img.shape[:2]) > target_size:
            h, w = img.shape[:2]
            scale = target_size / max(h, w)
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        return img


def _img_to_base64(img: np.ndarray, fmt=".png") -> str:
    if fmt == ".jpg":
        params = [cv2.IMWRITE_JPEG_QUALITY, 98]
    else:
        params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
    _, buf = cv2.imencode(fmt, img, params)
    return base64.b64encode(buf).decode("utf-8")


def _runtime_mask_dir() -> Path:
    path = _runtime_temp_lut_dir() / "masks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _runtime_depth_dir() -> Path:
    path = _runtime_temp_lut_dir() / "depth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_session_depth_meta(session_dir: str, strength: float, source_path: str) -> None:
    meta = {
        "strength": float(strength),
        "source_path": source_path or "",
        "created_at": datetime.now(USER_SPACE_TZ).isoformat(),
    }
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    (Path(session_dir) / "depth_layers.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _apply_cached_depth_layers_if_any(target_img: np.ndarray, result_img: np.ndarray, session_dir: Optional[str]) -> np.ndarray:
    if not session_dir or not os.path.isdir(session_dir):
        return result_img
    depth_path = os.path.join(session_dir, "depth_layers.png")
    meta_path = os.path.join(session_dir, "depth_layers.json")
    if not os.path.exists(depth_path):
        return result_img
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    strength = float(meta.get("strength", 0.65))
    depth = load_depth_file(depth_path, target_img.shape[:2])
    if depth is None:
        return result_img
    return apply_depth_layer_blend(target_img, result_img, depth, strength=strength)


def _write_session_subject_mask_meta(session_dir: str, mode: str, strength: float, source_path: str) -> None:
    meta = {
        "mode": str(mode or "none").lower(),
        "strength": float(strength),
        "source_path": source_path or "",
        "created_at": datetime.now(USER_SPACE_TZ).isoformat(),
    }
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    (Path(session_dir) / "subject_mask.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _apply_cached_subject_mask_if_any(target_img: np.ndarray, result_img: np.ndarray, session_dir: Optional[str]) -> np.ndarray:
    if not session_dir or not os.path.isdir(session_dir):
        return result_img
    mask_path = os.path.join(session_dir, "subject_mask.png")
    meta_path = os.path.join(session_dir, "subject_mask.json")
    if not os.path.exists(mask_path):
        return result_img
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    mode = str(meta.get("mode") or "none").lower()
    if mode == "none":
        return result_img
    strength = float(meta.get("strength", 1.0))
    subject_mask = load_mask_file(mask_path, target_img.shape[:2])
    if subject_mask is None:
        return result_img
    return apply_subject_mask_blend(target_img, result_img, subject_mask, mode, strength)


@app.post("/__legacy/api/depth/layers")
async def api_depth_layers(
    target_path: str = Form(...),
    depth_model: str = Form("auto"),
):
    depth_choice = _resolve_depth_model_choice(depth_model)
    resolved_target_path = _resolve_local_file_path(target_path)
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标图片不存在")
    target_path = str(resolved_target_path)

    cache_key = build_depth_cache_key(target_path, depth_choice["choice"])
    depth_dir = _runtime_depth_dir()
    depth_path = depth_dir / f"{cache_key}_depth.png"
    meta_path = depth_dir / f"{cache_key}_depth.json"
    cached = depth_path.exists()

    if cached:
        meta = {}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    else:
        target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=1536)
        if target_img is None:
            raise HTTPException(status_code=400, detail="无法读取目标图片")
        depth, meta = await asyncio.to_thread(generate_depth_map, target_img, BASE_DIR, depth_choice["choice"])
        await asyncio.to_thread(save_depth_png, depth, str(depth_path))
        meta.update({
            "cache_key": cache_key,
            "width": int(target_img.shape[1]),
            "height": int(target_img.shape[0]),
        })
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    token = int(time.time() * 1000)
    return JSONResponse({
        "success": True,
        "depth_id": cache_key,
        "depth_path": str(depth_path),
        "depth_url": f"/temp_luts/depth/{depth_path.name}?t={token}",
        "cached": cached,
        "model": depth_choice,
        "meta": meta,
    })


@app.post("/__legacy/api/semantic/match")
async def api_semantic_match(
    target_path: str = Form(...),
    reference_path: str = Form(None),
    reference: UploadFile = File(None),
    semantic_model: str = Form("auto"),
):
    semantic_choice = _resolve_semantic_model_choice(semantic_model)
    resolved_target_path = _resolve_local_file_path(target_path)
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标图片不存在")
    target_path = str(resolved_target_path)
    if reference is not None and reference.filename:
        reference_path = _save_upload(reference)
    resolved_reference_path = _resolve_local_file_path(reference_path)
    if not resolved_reference_path:
        raise HTTPException(status_code=400, detail="参考图片不存在")
    reference_path = str(resolved_reference_path)

    cache_key = build_semantic_cache_key(target_path, reference_path, semantic_choice["choice"])
    target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=1536)
    reference_img = await asyncio.to_thread(_cv2_imread, reference_path, target_size=1536)
    if target_img is None or reference_img is None:
        raise HTTPException(status_code=400, detail="无法读取目标图或参考图")
    meta = await asyncio.to_thread(summarize_semantic_matches, target_img, reference_img, semantic_choice["choice"])
    return JSONResponse({
        "success": True,
        "semantic_id": cache_key,
        "reference_path": reference_path,
        "model": semantic_choice,
        "meta": meta,
    })


@app.post("/__legacy/api/mask/subject")
async def api_subject_mask(
    target_path: str = Form(...),
    mode: str = Form("subject"),
    points_json: str = Form("[]"),
    mask_model: str = Form("auto"),
):
    mask_choice = _resolve_mask_model_choice(mask_model)
    prefer_birefnet = bool(mask_choice.get("prefer_birefnet"))
    resolved_target_path = _resolve_local_file_path(target_path)
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标图片不存在")
    target_path = str(resolved_target_path)
    try:
        points = json.loads(points_json or "[]")
        if not isinstance(points, list):
            points = []
    except Exception:
        points = []

    cache_key = build_mask_cache_key(target_path, mode, points, mask_choice["choice"])
    mask_dir = _runtime_mask_dir()
    mask_path = mask_dir / f"{cache_key}_mask.png"
    meta_path = mask_dir / f"{cache_key}_mask.json"
    cached = mask_path.exists()

    if cached:
        meta = {}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    else:
        target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=1536)
        if target_img is None:
            raise HTTPException(status_code=400, detail="无法读取目标图片")
        mask, meta = await asyncio.to_thread(
            generate_subject_mask,
            target_img,
            mode,
            points,
            prefer_birefnet,
            mask_choice["choice"],
        )
        await asyncio.to_thread(save_mask_png, mask, str(mask_path))
        meta.update({
            "cache_key": cache_key,
            "width": int(target_img.shape[1]),
            "height": int(target_img.shape[0]),
        })
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    token = int(time.time() * 1000)
    return JSONResponse({
        "success": True,
        "mask_id": cache_key,
        "mask_path": str(mask_path),
        "mask_url": f"/temp_luts/masks/{mask_path.name}?t={token}",
        "cached": cached,
        "model": mask_choice,
        "meta": meta,
    })


def _make_thread_callback(task_id, progress_start, progress_end, loop):
    def callback(stage, fraction, message=""):
        pct = progress_start + fraction * (progress_end - progress_start)
        future = asyncio.run_coroutine_threadsafe(
            progress_manager.send(task_id, stage, int(pct), message),
            loop,
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass
    return callback


def _wait_or_cancel_training(task_id: str):
    while progress_manager.is_paused(task_id) and not progress_manager.is_cancelled(task_id):
        time.sleep(0.2)
    if progress_manager.is_cancelled(task_id):
        raise RuntimeError("训练任务已取消")


async def _run_training_task(task_id: str, stage: str, image_dir: str, epochs: int, batch_size: int, lr: float, user_id: Optional[int] = None, user_role: str = "", target_model: str = "neuralpreset"):
    start_time = time.time()
    loop = asyncio.get_running_loop()
    training_path = _resolve_training_dir(image_dir)
    if not training_path.exists() or not training_path.is_dir():
        await progress_manager.send(task_id, "error", 0, "训练目录不存在")
        _write_task_log(
            task_id=task_id,
            task_type="模型训练",
            event_type="result",
            status="fail",
            summary="训练目录不存在",
            detail=str(training_path),
            user_id=user_id,
            role=user_role,
            model=target_model,
        )
        return

    stats = _get_training_data_stats_payload(str(training_path))
    file_count = int(stats["training_file_count"])
    if file_count == 0:
        await progress_manager.send(task_id, "error", 0, "训练目录中没有可用图片")
        _write_task_log(
            task_id=task_id,
            task_type="模型训练",
            event_type="result",
            status="fail",
            summary="训练目录中没有可用图片",
            detail=str(training_path),
            user_id=user_id,
            role=user_role,
            model=target_model,
        )
        return

    await progress_manager.send(task_id, "prepare", 2, f"训练数据已就绪，共 {file_count} 张")

    def send_stage_progress(stage_name: str, epoch_value: float, total_epochs: int, loss_value: float, start_pct: float, end_pct: float):
        fraction = 0 if total_epochs <= 0 else max(0.0, min(epoch_value / total_epochs, 1.0))
        pct = start_pct + (end_pct - start_pct) * fraction
        message = f"{stage_name} Epoch {epoch_value:.1f}/{total_epochs}，Loss {loss_value:.6f}"
        future = asyncio.run_coroutine_threadsafe(
            progress_manager.send(
                task_id,
                "training",
                pct,
                message,
                loss=round(float(loss_value), 6),
                epoch=f"{min(int(epoch_value), total_epochs)} / {total_epochs}",
                eta="训练中...",
                elapsed=round(time.time() - start_time, 1),
            ),
            loop,
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass

    try:
        from algorithms.dncm import train_normalization_stage, train_stylization_stage

        if stage in ("both", "norm"):
            await progress_manager.send(task_id, "training", 6, "开始归一化阶段训练", epoch=f"0 / {epochs}", eta="训练中...", elapsed=0)
            await asyncio.to_thread(
                train_normalization_stage,
                str(training_path),
                str(NEURALPRESET_MODEL_DIR),
                epochs,
                batch_size,
                lr,
                256,
                None,
                lambda epoch_value, total_epochs, loss_value: send_stage_progress("归一化阶段", epoch_value, total_epochs, loss_value, 6, 48),
                lambda: _wait_or_cancel_training(task_id),
            )

        if stage in ("both", "style"):
            norm_path = str(NEURALPRESET_NORM_WEIGHTS)
            if not os.path.exists(norm_path):
                raise RuntimeError("请先训练归一化阶段")
            await progress_manager.send(task_id, "training", 52, "开始风格阶段训练", epoch=f"0 / {epochs}", eta="训练中...", elapsed=round(time.time() - start_time, 1))
            await asyncio.to_thread(
                train_stylization_stage,
                str(training_path),
                norm_path,
                str(NEURALPRESET_MODEL_DIR),
                epochs,
                batch_size,
                lr,
                256,
                None,
                lambda epoch_value, total_epochs, loss_value: send_stage_progress("风格阶段", epoch_value, total_epochs, loss_value, 52, 96),
                lambda: _wait_or_cancel_training(task_id),
            )

        elapsed = round(time.time() - start_time, 1)
        await progress_manager.send(task_id, "done", 100, "训练完成", epoch=f"{epochs} / {epochs}", eta="已完成", elapsed=elapsed)
        _write_task_log(
            task_id=task_id,
            task_type="模型训练",
            event_type="result",
            status="ok",
            summary="训练完成",
            detail=f"阶段: {stage}，样本数: {file_count}",
            user_id=user_id,
            role=user_role,
            model=target_model,
            duration_ms=int(elapsed * 1000),
            meta={"stage": stage, "epochs": epochs, "batch_size": batch_size, "lr": lr, "training_file_count": file_count},
        )
    except Exception as e:
        if progress_manager.is_cancelled(task_id):
            await progress_manager.send(task_id, "cancelled", 0, "训练任务已取消", elapsed=round(time.time() - start_time, 1))
            _write_task_log(
                task_id=task_id,
                task_type="模型训练",
                event_type="control",
                status="cancel",
                summary="训练任务已取消",
                detail=str(training_path),
                user_id=user_id,
                role=user_role,
                model=target_model,
                duration_ms=int((time.time() - start_time) * 1000),
            )
        else:
            await progress_manager.send(task_id, "error", 0, str(e), elapsed=round(time.time() - start_time, 1))
            _write_task_log(
                task_id=task_id,
                task_type="模型训练",
                event_type="result",
                status="fail",
                summary=str(e)[:120],
                detail=str(e),
                user_id=user_id,
                role=user_role,
                model=target_model,
                duration_ms=int((time.time() - start_time) * 1000),
                meta={"stage": stage, "epochs": epochs, "batch_size": batch_size, "lr": lr},
            )
ALL_ALGORITHMS = {
    "reinhard": {
        "name": "经典追色-快速",
        "description": "经典 LAB 空间统计迁移，速度快，适合整体色调调整",
        "speed": "极快",
        "quality": "中等",
        "type": "traditional",
    },
    "histogram": {
        "name": "直方图追色-精确",
        "description": "逐通道直方图匹配，色彩分布更精确",
        "speed": "快",
        "quality": "良好",
        "type": "traditional",
    },
    "luminance_partition": {
        "name": "亮度分区追色-自然",
        "description": "按高光/中间调/阴影分区迁移，效果最自然",
        "speed": "较快",
        "quality": "优秀",
        "type": "traditional",
    },
    "neural_preset": {
        "name": "神经预设追色-需训练",
        "description": "基于 CVPR 2023 论文的确定性神经色彩映射，无伪影，支持4K实时",
        "speed": "快(需GPU)",
        "quality": "顶级",
        "type": "neural",
    },
    "modflows": {
        "name": "AI全局追色-高保真",
        "description": "基于 AAAI 2025 论文的调制流色彩迁移，当前 SOTA，预训练模型可直接使用",
        "speed": "中等(ODE迭代)",
        "quality": "最强(SOTA)",
        "type": "neural",
    },
    "modflows_b0": {
        "name": "AI全局追色-B0快速模式",
        "description": "ModFlows 轻量版快速追色模式，适合快速预览、低配设备和批量初筛",
        "speed": "较快",
        "quality": "良好",
        "type": "neural",
    },
    "regional_modflows": {
        "name": "AI分区追色-皮肤保护",
        "description": "ModFlows + MediaPipe语义分割分区追色，皮肤/天空/高光/阴影独立控制，最接近像素蛋糕效果",
        "speed": "中等",
        "quality": "最强(商业级)",
        "type": "neural",
    },
    "regional_luminance": {
        "name": "快速分区追色-皮肤保护",
        "description": "亮度分区迁移 + 语义分割后处理，速度快，效果好",
        "speed": "快",
        "quality": "优秀",
        "type": "traditional",
    },
    "dncm_lut": {
        "name": "神经预设追色-精确LUT",
        "description": "DNCM 3x3矩阵映射 + 全色彩空间LUT网格采样，确定性精确追色，像素蛋糕同源技术",
        "speed": "快",
        "quality": "顶级(像素级)",
        "type": "neural",
    },
    "ai_portrait": {
        "name": "AI人像追色-肤色保护",
        "description": "单LUT + Lab肤色洗脱 + Mask上采样混合，商业级人像追色方案",
        "speed": "中等",
        "quality": "最强(商业级)",
        "type": "neural",
    },
    "import_lut": {
        "name": "导入LUT调色",
        "description": "直接导入外部 .cube/.3dl/.csp/.spi3d/.spi1d/.lut 文件进行极速LUT查表渲染",
        "speed": "极快(纯查表)",
        "quality": "取决于LUT",
        "type": "lut",
    },
}

app.include_router(create_meta_router(BASE_DIR, ALL_ALGORITHMS), tags=["meta"])
app.include_router(
    create_analysis_router(
        BASE_DIR,
        _resolve_depth_model_choice,
        _resolve_semantic_model_choice,
        _resolve_mask_model_choice,
        _resolve_local_file_path,
        _runtime_depth_dir,
        _runtime_mask_dir,
        _cv2_imread,
        generate_depth_map,
        save_depth_png,
        build_depth_cache_key,
        generate_subject_mask,
        save_mask_png,
        build_mask_cache_key,
        build_semantic_cache_key,
        summarize_semantic_matches,
        _save_upload,
    ),
    tags=["analysis"],
)
app.include_router(
    create_files_router(
        _ensure_project_access,
        _runtime_video_dir,
        _runtime_temp_lut_dir,
    ),
    tags=["files"],
)
app.include_router(lut_router, tags=["lut"])
app.include_router(styles_router, tags=["styles"])
app.include_router(create_progress_router(progress_manager), tags=["progress"])
app.include_router(
    create_training_router(
        progress_manager=progress_manager,
        get_request_user_id=_get_request_user_id,
        get_request_user_role=_get_request_user_role,
        write_task_log=_write_task_log,
        get_training_data_stats_payload=_get_training_data_stats_payload,
        resolve_training_dir=_resolve_training_dir,
        training_image_extensions=TRAINING_IMAGE_EXTENSIONS,
        run_training_task=_run_training_task,
        neuralpreset_model_dir=NEURALPRESET_MODEL_DIR,
    ),
    tags=["training"],
)
app.include_router(
    create_model_status_router(
        base_dir=BASE_DIR,
        modflows_b6_checkpoint=MODFLOWS_B6_CHECKPOINT,
        modflows_b0_checkpoint=MODFLOWS_B0_CHECKPOINT,
        get_neuralpreset_weight_status=get_neuralpreset_weight_status,
        get_subject_mask_status=get_subject_mask_status,
        get_birefnet_status=get_birefnet_status,
        get_depth_anything_status=get_depth_anything_status,
        get_dinov2_status=get_dinov2_status,
        merge_public_model_management=_merge_public_model_management,
    ),
    tags=["model-status"],
)
app.include_router(create_task_router(_get_request_user_id, _get_request_user_role, _write_task_log), tags=["tasks"])


@app.post("/api/upload_batch")
async def api_upload_batch(
    files: List[UploadFile] = File(...),
    project_id: int = Form(0),
    authorization: Optional[str] = Header(None),
):
    request_user_id = _get_request_user_id(authorization)
    project_id = await _ensure_project_access(project_id, request_user_id)
    results = []
    for file in files:
        if not file.filename:
            continue
        ensure_upload_file_size(file, 10 * 1024 * 1024, label="普通上传文件")
        ext = os.path.splitext(file.filename)[1] or ".jpg"
        uid = uuid.uuid4().hex
        save_name = f"{uid}{ext}"
        if project_id > 0:
            save_dir = _safe_project_bucket_dir(project_id, "source")
            thumb_dir = _safe_project_bucket_dir(project_id, "thumbs")
            save_path = save_dir / save_name
            asset_url = f"/api/project_assets/{project_id}/source/{save_name}"
        else:
            save_path = Path(USER_IMAGES_DIR) / save_name
            thumb_dir = Path(USER_IMAGES_DIR)
            asset_url = f"/api/user_assets/images/{save_name}?t={uid}"

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"上传文件为空: {file.filename}")
        with open(save_path, "wb") as f:
            f.write(content)
        await _ensure_training_target_sample(
            user_id=request_user_id,
            target_path=str(save_path),
            project_id=project_id,
            asset_name=file.filename or save_name,
        )

        try:
            img = await asyncio.to_thread(_cv2_imread, str(save_path), target_size=512)
        except Exception:
            img = cv2.imread(str(save_path))
        if img is None:
            img = np.zeros((256, 256, 3), dtype=np.uint8)

        h, w = img.shape[:2]
        thumb_name = f"{uid}_thumb.jpg"
        thumb_path = thumb_dir / thumb_name
        thumb_img = img
        if max(w, h) > 256:
            scale = 256.0 / max(w, h)
            thumb_img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, thumb_buf = cv2.imencode('.jpg', thumb_img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            thumb_buf.tofile(thumb_path)

        results.append({
            "id": uid,
            "name": file.filename,
            "path": str(save_path),
            "asset_url": asset_url,
            "thumbnail": (
                f"/api/project_assets/{project_id}/thumbs/{thumb_name}?t={uid}"
                if project_id > 0
                else f"/api/user_assets/images/{thumb_name}?t={uid}"
            ),
            "project_saved": bool(project_id > 0),
            "meta": f"{w}×{h}",
        })
    return JSONResponse({"images": results})


@app.post("/api/apply_profile")
async def api_apply_profile(
    target_path: str = Form(...),
    session_id: str = Form(None),
    profile_file: UploadFile = File(None),
    profile_builtin: str = Form(None),
    project_id: int = Form(0),
    reference_path: str = Form(None),
    authorization: Optional[str] = Header(None),
):
    request_user_id = _get_request_user_id(authorization)
    request_user_role = _get_request_user_role(authorization)
    request_started_at = time.time()
    if request_user_id is not None:
        record_user_usage(request_user_id)
    project_id = await _ensure_project_access(project_id, request_user_id)
    resolved_target_path = _resolve_local_file_path(target_path)
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标图片不存在")
    target_path = str(resolved_target_path)

    target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=1024)
    if target_img is None:
        raise HTTPException(status_code=400, detail="无法读取目标图片")

    session_dir = None
    if session_id:
        session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
        os.makedirs(session_dir, exist_ok=True)

    if profile_builtin:
        record_model_call("neural_preset")
        lut_3d = await asyncio.to_thread(_generate_builtin_profile, profile_builtin)
    elif profile_file and profile_file.filename:
        ensure_upload_file_size(profile_file, 10 * 1024 * 1024, label="风格文件")
        record_model_call("neural_preset")
        lut_ext = os.path.splitext(profile_file.filename)[1].lower()
        lut_bytes = await profile_file.read()
        tmp_path = os.path.join(str(_runtime_temp_lut_dir()), f"profile_{uuid.uuid4().hex}{lut_ext}")
        with open(tmp_path, "wb") as f:
            f.write(lut_bytes)
        print(f"[PF3-DEBUG] file={profile_file.filename}, size={len(lut_bytes)} bytes, first 500 chars:")
        print(lut_bytes[:500].decode('utf-8', errors='replace'))
        from core.io.lut_parser import parse_lut_file
        lut_3d = await asyncio.to_thread(parse_lut_file, tmp_path)
    else:
        raise HTTPException(status_code=400, detail="请提供配置文件或选择预设")

    from core.render.full_render import apply_lut
    target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
    result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_3d)
    result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)

    if not session_dir:
        profile_session_id = uuid.uuid4().hex
        session_dir = os.path.join(str(_runtime_temp_lut_dir()), profile_session_id)
        os.makedirs(session_dir, exist_ok=True)

    lut_save_path = os.path.join(session_dir, "lut_global.npy")
    if os.path.exists(lut_save_path):
        os.remove(lut_save_path)
    await asyncio.to_thread(np.save, lut_save_path, lut_3d)

    result_b64 = await asyncio.to_thread(_img_to_base64, result_img, ".png")
    original_b64 = await asyncio.to_thread(_img_to_base64, target_img, ".png")
    result_lut_path = os.path.join(session_dir, "lut_global.npy")
    _write_task_log(
        task_id="",
        task_type="图片追色",
        event_type="result",
        status="ok",
        summary="配置应用完成",
        detail=profile_builtin or (profile_file.filename if profile_file else "custom_profile"),
        user_id=request_user_id,
        role=request_user_role,
        model=profile_builtin or "profile_file",
        duration_ms=int(max(0, (time.time() - request_started_at) * 1000)),
        meta={"source": "apply_profile"},
    )

    return JSONResponse({
        "success": True,
        "result_b64": f"data:image/png;base64,{result_b64}",
        "original_b64": f"data:image/png;base64,{original_b64}",
        "lut_path": result_lut_path,
    })


@app.post("/api/transfer")
async def api_transfer(
    target: UploadFile = File(None),
    reference: UploadFile = File(None),
    algorithm: str = Form("luminance_partition"),
    blend_strength: float = Form(0.85),
    enable_metrics: bool = Form(False),
    enable_postprocess: bool = Form(True),
    task_id: str = Form(None),
    profile_file: UploadFile = File(None),
    profile_builtin: str = Form(None),
    target_path: str = Form(None),
    generate_lut_only: bool = Form(False),
    lut_mode: str = Form("fast"),
    enable_semantic_match: bool = Form(False),
    semantic_model: str = Form("auto"),
    semantic_strength: float = Form(0.55),
    enable_depth_layers: bool = Form(False),
    depth_model: str = Form("auto"),
    depth_path: str = Form(None),
    depth_strength: float = Form(0.65),
    mask_path: str = Form(None),
    mask_model: str = Form("auto"),
    mask_mode: str = Form("none"),
    mask_strength: float = Form(1.0),
    project_id: int = Form(0),
    reference_path: str = Form(None),
    authorization: Optional[str] = Header(None),
):
    model_runtime = _resolve_transfer_model_runtime(algorithm)
    algorithm = model_runtime["algorithm"]
    disabled_model = model_runtime.get("model_key")
    if disabled_model and disabled_model in model_runtime["disabled"]:
        raise HTTPException(status_code=400, detail=_disabled_model_error(disabled_model))
    if enable_semantic_match:
        semantic_choice = _resolve_semantic_model_choice(semantic_model)
    else:
        semantic_choice = {"choice": "auto", "model_key": None}
    if enable_depth_layers:
        depth_choice = _resolve_depth_model_choice(depth_model)
    else:
        depth_choice = {"choice": "auto", "model_key": None}
    if str(mask_mode or "none").lower() != "none":
        mask_choice = _resolve_mask_model_choice(mask_model)
    else:
        mask_choice = {"choice": "auto", "model_key": None, "prefer_birefnet": True}

    if algorithm not in ALL_ALGORITHMS:
        raise HTTPException(status_code=400, detail=f"不支持的算法: {algorithm}")

    request_user_id = _get_request_user_id(authorization)
    request_user_role = _get_request_user_role(authorization)
    project_id = await _ensure_project_access(project_id, request_user_id)
    request_started_at = time.time()
    trace_started_at = time.perf_counter()
    trace_last_at = trace_started_at

    def trace_mark(label: str, **fields):
        nonlocal trace_last_at
        now = time.perf_counter()
        total_ms = int((now - trace_started_at) * 1000)
        delta_ms = int((now - trace_last_at) * 1000)
        trace_last_at = now
        meta = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        print(
            f"[PERF][transfer][task={task_id or '-'}] {label} "
            f"total_ms={total_ms} delta_ms={delta_ms} "
            f"algorithm={algorithm} lut_only={int(bool(generate_lut_only))} {meta}".rstrip()
        )

    trace_mark("request_start")

    async def trace_to_thread(label: str, func, *args, **kwargs):
        trace_mark(f"{label}_start")
        try:
            result = await asyncio.to_thread(func, *args, **kwargs)
            trace_mark(f"{label}_done")
            return result
        except Exception as exc:
            trace_mark(f"{label}_error", error=type(exc).__name__)
            raise

    if request_user_id is not None:
        record_user_usage(request_user_id)

    recorded_model_call = False

    def mark_model_call(name):
        nonlocal recorded_model_call
        if recorded_model_call:
            return
        record_model_call(name)
        recorded_model_call = True

    recorded_task_outcome = False

    def mark_task_success():
        nonlocal recorded_task_outcome
        if recorded_task_outcome:
            return
        record_task_outcome(True)
        _write_task_log(
            task_id=task_id or "",
            task_type="图片追色",
            event_type="result",
            status="ok",
            summary="图片追色完成",
            detail=ALL_ALGORITHMS.get(algorithm, {}).get("name", algorithm),
            user_id=request_user_id,
            role=request_user_role,
            model=algorithm,
            duration_ms=int(max(0, (time.time() - request_started_at) * 1000)),
            meta={"enable_metrics": enable_metrics, "enable_postprocess": enable_postprocess},
        )
        recorded_task_outcome = True

    def mark_task_failure():
        nonlocal recorded_task_outcome
        if recorded_task_outcome:
            return
        record_task_outcome(False)
        _write_task_log(
            task_id=task_id or "",
            task_type="图片追色",
            event_type="result",
            status="fail",
            summary="图片追色失败",
            detail=ALL_ALGORITHMS.get(algorithm, {}).get("name", algorithm),
            user_id=request_user_id,
            role=request_user_role,
            model=algorithm,
            duration_ms=int(max(0, (time.time() - request_started_at) * 1000)),
        )
        recorded_task_outcome = True

    def raise_task_http_error(status_code, detail, *, count_failure=True):
        if count_failure:
            mark_task_failure()
        raise HTTPException(status_code=status_code, detail=detail)

    pm = progress_manager
    has_progress = task_id is not None
    if has_progress:
        pm.register_task(task_id)
    request_user_id = _get_request_user_id(authorization)

    async def prog(stage, progress, message=""):
        if has_progress:
            await pm.send(task_id, stage, progress, message)

    async def wait_if_paused():
        if has_progress:
            while pm.is_paused(task_id) and not pm.is_cancelled(task_id):
                await asyncio.sleep(0.5)
            if pm.is_cancelled(task_id):
                await prog("cancelled", 0, "任务已取消")
                raise_task_http_error(499, "任务已被用户取消", count_failure=False)

    loop = asyncio.get_event_loop()

    await wait_if_paused()
    await prog("upload", 5, "读取图片中...")
    await asyncio.sleep(0.01)

    target_load_size = 2048 if not generate_lut_only else None
    if algorithm == "dncm_lut" and str(lut_mode or "").lower() == "fast":
        target_load_size = 1024

    resolved_target_path = _resolve_local_file_path(target_path)
    if resolved_target_path and resolved_target_path.exists():
        target_path = str(resolved_target_path)
        target_img = await trace_to_thread("load_target", _cv2_imread, target_path, target_size=target_load_size)
        if target_img is None:
            raise_task_http_error(400, "无法读取目标图片路径")
    elif target is not None and target.filename:
        ensure_upload_file_size(target, 300 * 1024 * 1024, label="追色原图")
        target_path = _save_upload(target, project_id=project_id, bucket="source")
        await _ensure_training_target_sample(
            user_id=request_user_id,
            target_path=target_path,
            project_id=project_id,
            asset_name=target.filename,
        )
        target_img = await trace_to_thread("load_target", _cv2_imread, target_path, target_size=target_load_size)
    else:
        await prog("error", 0, "未提供目标图片")
        raise_task_http_error(400, "请提供目标图片")

    reference_img = None
    resolved_reference_path = _resolve_local_file_path(reference_path)
    if resolved_reference_path and resolved_reference_path.exists():
        reference_path = str(resolved_reference_path)
        reference_img = await trace_to_thread("load_reference", _cv2_imread_full, reference_path)
    elif reference is not None and reference.filename:
        ensure_upload_file_size(reference, 10 * 1024 * 1024, label="参考图")
        reference_path = _save_upload(reference, project_id=project_id, bucket="reference")
        reference_img = await trace_to_thread("load_reference", _cv2_imread_full, reference_path)

    if reference_img is None and (profile_file is None or not profile_file.filename) and not generate_lut_only:
        await prog("error", 0, "无法读取参考图片")
        raise_task_http_error(400, "无法读取参考图片")

    await prog("upload", 15, f"图片已加载 {target_img.shape[1]}x{target_img.shape[0]}")
    await asyncio.sleep(0.01)
    await wait_if_paused()

    orig_h, orig_w = target_img.shape[:2]
    start_time = time.time()

    await prog("analyze", 20, "分析图像特征...")
    await asyncio.sleep(0.01)
    await wait_if_paused()

    if algorithm == "neural_preset":
        await prog("transfer", 25, "Neural Preset 推理中...")
        await asyncio.sleep(0.01)
        try:
            mark_model_call("neural_preset")
            result_img = await trace_to_thread(
                "neural_preset_transfer",
                neural_preset_transfer, target_img, reference_img,
                model_dir=str(NEURALPRESET_MODEL_DIR)
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"Neural Preset 推理失败: {str(e)}")
    elif algorithm == "modflows":
        await prog("encode", 25, "ModFlows 初始化...")
        await asyncio.sleep(0.01)
        try:
            mark_model_call(model_runtime["modflows_key"])
            cb = _make_thread_callback(task_id, 25, 60, loop) if has_progress else None
            result_img = await trace_to_thread(
                "modflows_transfer",
                modflows_transfer,
                target_img, reference_img,
                encoder_type=model_runtime["modflows_encoder"], steps=16, strength=1.0,
                progress_callback=cb,
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"ModFlows 推理失败: {str(e)}")

        await prog("color_refine", 72, "色彩分布精调...")
        await asyncio.sleep(0.01)
        try:
            from core.color.color_refine import refine_color_distribution
            result_img = await trace_to_thread(
                "modflows_color_refine",
                refine_color_distribution, result_img, reference_img, 0.7
            )
            print("[Color Refine] Applied to modflows (strength=0.7)")
        except Exception as e:
            print(f"[Color Refine] refine failed: {e}")

        await prog("cinematic", 76, "光影重塑中...")
        await asyncio.sleep(0.01)
        try:
            from core.color.cinematic_enhance import reshape_lighting, add_film_grain
            result_img = await trace_to_thread(
                "cinematic_reshape_lighting",
                reshape_lighting, result_img, reference_img, 0.6
            )
            result_img = await trace_to_thread("cinematic_add_film_grain", add_film_grain, result_img)
            print("[Cinematic] reshape_lighting + add_film_grain applied")
        except Exception as e:
            print(f"[Cinematic] failed: {e}")
    elif algorithm == "modflows_b0":
        await prog("encode", 25, "ModFlows B0 快速模式...")
        await asyncio.sleep(0.01)
        try:
            if not model_runtime["modflows_b0_enabled"]:
                raise RuntimeError(_disabled_model_error("modflows_b0"))
            mark_model_call("modflows_b0")
            cb = _make_thread_callback(task_id, 25, 58, loop) if has_progress else None
            result_img = await trace_to_thread(
                "modflows_b0_transfer",
                modflows_transfer,
                target_img, reference_img,
                encoder_type="B0", steps=6, strength=0.75,
                progress_callback=cb,
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"ModFlows B0 快速模式推理失败: {str(e)}")

        await prog("color_refine", 68, "快速端色彩修正...")
        await asyncio.sleep(0.01)
        try:
            from core.color.color_refine import refine_color_distribution
            result_img = await trace_to_thread(
                "modflows_b0_color_refine",
                refine_color_distribution, result_img, reference_img, 0.45
            )
        except Exception as e:
            print(f"[ModFlows B0] refine failed: {e}")
    elif algorithm == "regional_modflows":
        await prog("segment", 25, "分区追色 (ModFlows) 启动...")
        await asyncio.sleep(0.01)
        try:
            mark_model_call(model_runtime["modflows_key"])
            cb = _make_thread_callback(task_id, 25, 70, loop) if has_progress else None
            inner_cb = _make_thread_callback(task_id, 25, 50, loop) if has_progress else None
            result_img = await trace_to_thread(
                "regional_modflows_transfer",
                regional_transfer,
                target_img, reference_img,
                transfer_func=lambda t, r: modflows_transfer(
                    t, r, encoder_type=model_runtime["modflows_encoder"], steps=16, strength=1.0,
                    progress_callback=inner_cb,
                ),
                base_strength=blend_strength,
                progress_callback=cb,
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"分区追色(ModFlows)失败: {str(e)}")

        await prog("color_refine", 72, "色彩分布精调...")
        await asyncio.sleep(0.01)
        try:
            from core.color.color_refine import refine_color_distribution
            result_img = await trace_to_thread(
                "regional_modflows_color_refine",
                refine_color_distribution, result_img, reference_img, 0.7
            )
            print("[Color Refine] Applied to regional_modflows (strength=0.7)")
        except Exception as e:
            print(f"[Color Refine] refine failed: {e}")
    elif algorithm == "regional_luminance":
        await prog("segment", 25, "分区追色 (亮度分区) 启动...")
        await asyncio.sleep(0.01)
        try:
            cb = _make_thread_callback(task_id, 25, 70, loop) if has_progress else None
            result_img = await trace_to_thread(
                "regional_luminance_transfer",
                regional_transfer,
                target_img, reference_img,
                transfer_func=lambda t, r: transfer_color(t, r, algorithm="luminance_partition", blend_strength=1.0),
                base_strength=blend_strength,
                progress_callback=cb,
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"分区追色(亮度分区)失败: {str(e)}")
    elif algorithm == "ai_portrait":
        session_id = str(uuid.uuid4())

        _debug_dir = os.path.join(str(BASE_DIR), "debug_output")
        os.makedirs(_debug_dir, exist_ok=True)

        def _debug_save(img, name):
            try:
                path = os.path.join(_debug_dir, name)
                _, buf = cv2.imencode(".jpg" if name.endswith(".jpg") else ".png", img)
                with open(path, "wb") as f:
                    f.write(buf.tobytes())
                print(f"[DEBUG] Saved {path}")
            except Exception as e:
                print(f"[DEBUG] Failed to save {name}: {e}")

        await asyncio.to_thread(_debug_save, reference_img, "0_reference.jpg")

        engine_used = "modflows"
        await prog("ai_transfer", 25, "AI全局追色 (NeuralPreset) 启动...")
        await asyncio.sleep(0.01)
        try:
            if not model_runtime["ai_portrait_neural_enabled"]:
                raise RuntimeError("AI Portrait NeuralPreset 已在后台禁用")
            from algorithms.neuralpreset.adapter import neuralpreset_transfer
            mark_model_call("ai_portrait_neuralpreset")
            result_global = await asyncio.to_thread(
                neuralpreset_transfer,
                target_img, reference_img, "cpu",
            )
            engine_used = "neuralpreset"
            print("[NeuralPreset] Transfer successful")
            await asyncio.to_thread(_debug_save, result_global, "1_neuralpreset_raw.jpg")
        except Exception as e:
            print(f"[NeuralPreset] Failed ({e}), falling back to ModFlows")
            await prog("ai_transfer", 25, "AI全局追色 (ModFlows fallback) 启动...")
            await asyncio.sleep(0.01)
            try:
                if not model_runtime["modflows_enabled"]:
                    raise RuntimeError(f"后台默认 ModFlows 已禁用：{model_runtime['modflows_key']}")
                mark_model_call(model_runtime["modflows_key"])
                cb = _make_thread_callback(task_id, 25, 55, loop) if has_progress else None
                result_global = await asyncio.to_thread(
                    modflows_transfer,
                    target_img, reference_img,
                    encoder_type=model_runtime["modflows_encoder"], steps=16, strength=1.0,
                    progress_callback=cb,
                )
                engine_used = "modflows"
                print("[ModFlows] Fallback successful")
                await asyncio.to_thread(_debug_save, result_global, "1_modflows_raw.jpg")
            except Exception as e2:
                await prog("error", 0, str(e2))
                raise_task_http_error(500, f"AI人像追色失败: {str(e2)}")

        await prog("color_refine", 56, "Lab全局精调(全色调拉向参考图)...")
        await asyncio.sleep(0.01)
        try:
            from core.color.color_refine import refine_color_distribution
            result_img = await asyncio.to_thread(
                refine_color_distribution, result_global, reference_img,
                l_mean_strength=0.3, a_mean_strength=0.0, b_mean_strength=0.8,
                l_std_strength=0.25, a_std_strength=0.15, b_std_strength=0.3
            )
            print("[Color Refine] L_mean=0.3 a_mean=0.0 b_mean=0.8 L_std=0.25 a_std=0.15 b_std=0.3")
        except Exception as e:
            print(f"[Color Refine] failed: {e}, using raw ModFlows output")
            result_img = result_global

        await prog("face_segment", 62, "SegFace 人脸语义分割...")
        await asyncio.sleep(0.01)
        has_skin = False
        skin_mask = None
        lip_mask = None
        hair_mask = None
        try:
            if not model_runtime["segface_enabled"]:
                raise RuntimeError("SegFace 已在后台禁用")
            from algorithms.segface import parse_face_semantics
            from core.color.lut_extractor import extract_lut_from_pair

            masks = await asyncio.to_thread(parse_face_semantics, target_img)
            skin_mask = masks['skin']
            lip_mask = masks['lip']
            hair_mask = masks['hair']

            skin_pct = skin_mask.sum() / skin_mask.size * 100
            print(f"[SegFace] skin: {skin_pct:.1f}%, lip: {lip_mask.sum()/lip_mask.size*100:.1f}%, hair: {hair_mask.sum()/hair_mask.size*100:.1f}%")
            has_skin = skin_pct > 2.0

            session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
            os.makedirs(session_dir, exist_ok=True)

            def _save_mask(p, m):
                ok, buf = cv2.imencode(".png", (m * 255).astype(np.uint8))
                if ok:
                    with open(p, "wb") as f:
                        f.write(buf.tobytes())

            await asyncio.to_thread(_save_mask,
                os.path.join(session_dir, "mask_soft.png"), skin_mask)
            await asyncio.to_thread(_save_mask,
                os.path.join(session_dir, "lip_mask.png"), lip_mask)
            await asyncio.to_thread(_save_mask,
                os.path.join(session_dir, "hair_mask.png"), hair_mask)
            await asyncio.to_thread(_debug_save,
                (skin_mask * 255).astype(np.uint8), "3_skin_mask.png")
            await asyncio.to_thread(_debug_save,
                (lip_mask * 255).astype(np.uint8), "4_lip_mask.png")
            await asyncio.to_thread(_debug_save,
                (hair_mask * 255).astype(np.uint8), "5_hair_mask.png")

            ref_f = reference_img.astype(np.float32) / 255.0
            ref_lab = cv2.cvtColor(ref_f, cv2.COLOR_BGR2Lab)
            _, a_ref_full, b_ref_full = cv2.split(ref_lab)
            ref_hsv = cv2.cvtColor(reference_img, cv2.COLOR_BGR2HSV).astype(np.float32)
            ref_stats = np.array([
                float(a_ref_full.mean()), float(b_ref_full.mean()),
                float(ref_hsv[:, :, 2].mean()),
            ], dtype=np.float32)
            await asyncio.to_thread(np.save,
                os.path.join(session_dir, "ref_stats.npy"), ref_stats)
            print(f"[Ref Stats] a={ref_stats[0]:.1f} b={ref_stats[1]:.1f} V={ref_stats[2]:.0f}")
        except Exception as e:
            print(f"[SegFace] segmentation failed: {e}")
            has_skin = False

        if has_skin and skin_mask is not None:
            await prog("skin_reconstruct", 68, "肤色重建(保留血色底子)...")
            await asyncio.sleep(0.01)
            try:
                from core.color.skin_protect import reconstruct_clean_skin, preserve_makeup, colorize_highlights

                result_img, _ = await asyncio.to_thread(
                    reconstruct_clean_skin,
                    result_img=result_img,
                    source_img=target_img,
                    mask_skin=skin_mask,
                    strength=0.7,
                )
                print("[Skin Reconstruct] applied in preview")

                await prog("makeup_preserve", 72, "妆容保护(精准唇部增强)...")
                await asyncio.sleep(0.01)
                ref_hsv = cv2.cvtColor(reference_img, cv2.COLOR_BGR2HSV).astype(np.float32)
                v_ref_avg = float(ref_hsv[:, :, 2].mean())
                result_img = await asyncio.to_thread(
                    preserve_makeup,
                    source_img=target_img,
                    result_img=result_img,
                    lip_mask=lip_mask,
                    saturation_boost=1.4,
                    v_ref_avg=v_ref_avg,
                )
                print("[Makeup] applied in preview")

                await prog("highlights", 76, "高光染色(青蓝调)...")
                await asyncio.sleep(0.01)
                result_img = await asyncio.to_thread(
                    colorize_highlights,
                    result_img=result_img,
                    hair_mask=hair_mask,
                )
                print("[Highlights] applied in preview")
            except Exception as e:
                print(f"[Skin/Makeup/Highlights] failed: {e}")

        await asyncio.to_thread(_debug_save, result_img, "2_final.jpg")

        try:
            from core.color.lut_extractor import extract_lut_from_pair
            target_rgb_lut = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            result_rgb_lut = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            lut_3d = await asyncio.to_thread(extract_lut_from_pair,
                target_rgb_lut, result_rgb_lut, 33)
            await asyncio.to_thread(np.save,
                os.path.join(session_dir, "lut_global.npy"), lut_3d)
            print("[LUT] extracted from final result (with skin/makeup/highlights)")
        except Exception as e:
            print(f"[LUT] extract failed: {e}")

        print("[DEBUG] Diagnostic images saved to debug_output/")
    elif algorithm == "dncm_lut":
        weight_status = get_neuralpreset_weight_status()
        if not weight_status["ready"]:
            searched = "；".join(weight_status["model_dirs"])
            missing = "、".join(weight_status["missing"])
            raise_task_http_error(
                400,
                    f"DNCM / NeuralPreset LUT 权重不完整，缺少 {missing}。请放入 model_assets/neural_preset；旧目录 models/neural_preset 或 weights/neuralpreset 仍会兼容读取。已查找：{searched}"
                )
        dncm_mode = "quality" if str(lut_mode or "").lower() in ("quality", "high", "hq") else "fast"
        lut_size = 33 if dncm_mode == "quality" else 17
        await prog("dncm_lut", 25, "DNCM LUT 生成中..." if dncm_mode == "fast" else "DNCM 高质量 LUT 生成中...")
        await asyncio.sleep(0.01)
        try:
            from algorithms.dncm import generate_lut_from_dncm
            from core.render.full_render import apply_lut

            lut_3d = await asyncio.to_thread(
                generate_lut_from_dncm, reference_img, target_img, lut_size, str(NEURALPRESET_MODEL_DIR)
            )

            target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_3d)
            result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)

            dncm_semantic_meta = None
            if enable_semantic_match:
                if not model_runtime["semantic_match_enabled"]:
                    raise ValueError(_disabled_model_error("dinov2_semantic_match"))
                await prog("semantic_match", 78, "应用参考图语义匹配...")
                await asyncio.sleep(0.01)
                result_img, dncm_semantic_meta = await trace_to_thread(
                    "semantic_match_transfer",
                    semantic_match_transfer,
                    target_img,
                    result_img,
                    reference_img,
                    semantic_strength,
                    semantic_choice["choice"],
                )

            dncm_applied_depth_path = ""
            if enable_depth_layers and depth_path:
                if not model_runtime["depth_layers_enabled"]:
                    raise ValueError(_disabled_model_error("depth_anything_v2"))
                await prog("depth_layers", 80, "应用深度分层追色...")
                await asyncio.sleep(0.01)
                depth_map = await trace_to_thread(
                    "load_depth_layers",
                    load_depth_file,
                    depth_path,
                    target_img.shape[:2],
                )
                if depth_map is None:
                    raise ValueError("depth 文件不存在或无法读取")
                result_img = await trace_to_thread(
                    "apply_depth_layers",
                    apply_depth_layer_blend,
                    target_img,
                    result_img,
                    depth_map,
                    depth_strength,
                )
                dncm_applied_depth_path = depth_path

            dncm_mask_mode = str(mask_mode or "none").lower()
            dncm_applied_mask_path = ""
            if dncm_mask_mode != "none" and mask_path:
                if not model_runtime["subject_mask_enabled"]:
                    raise ValueError(_disabled_model_error("sam_subject_mask"))
                await prog("mask_blend", 82, "应用区域保护 mask...")
                await asyncio.sleep(0.01)
                subject_mask = await trace_to_thread(
                    "load_subject_mask",
                    load_mask_file,
                    mask_path,
                    target_img.shape[:2],
                )
                if subject_mask is None:
                    raise ValueError("mask 文件不存在或无法读取")
                result_img = await trace_to_thread(
                    "apply_subject_mask",
                    apply_subject_mask_blend,
                    target_img,
                    result_img,
                    subject_mask,
                    dncm_mask_mode,
                    mask_strength,
                )
                dncm_applied_mask_path = mask_path

            session_lut_3d = lut_3d
            if dncm_semantic_meta or dncm_applied_depth_path or dncm_applied_mask_path:
                try:
                    from core.color.lut_extractor import extract_lut_from_pair
                    target_rgb_lut = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
                    result_rgb_lut = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
                    session_lut_3d = await trace_to_thread(
                        "extract_lut_dncm_enhanced",
                        extract_lut_from_pair,
                        target_rgb_lut,
                        result_rgb_lut,
                        33,
                    )
                except Exception as exc:
                    print(f"[DNCM] enhanced LUT extraction failed: {exc}")

            session_id = str(uuid.uuid4())
            session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
            os.makedirs(session_dir, exist_ok=True)
            if dncm_applied_depth_path:
                await trace_to_thread(
                    "copy_depth_layers",
                    shutil.copyfile,
                    dncm_applied_depth_path,
                    os.path.join(session_dir, "depth_layers.png"),
                )
                await trace_to_thread(
                    "write_depth_layers_meta",
                    _write_session_depth_meta,
                    session_dir,
                    depth_strength,
                    dncm_applied_depth_path,
                )
            if dncm_applied_mask_path:
                await trace_to_thread(
                    "copy_subject_mask",
                    shutil.copyfile,
                    dncm_applied_mask_path,
                    os.path.join(session_dir, "subject_mask.png"),
                )
                await trace_to_thread(
                    "write_subject_mask_meta",
                    _write_session_subject_mask_meta,
                    session_dir,
                    dncm_mask_mode,
                    mask_strength,
                    dncm_applied_mask_path,
                )
            lut_path = os.path.join(session_dir, "lut_global.npy")
            await asyncio.to_thread(np.save, lut_path, session_lut_3d)
            reusable_preset = await asyncio.to_thread(
                _save_lut_as_style_preset,
                session_lut_3d,
                result_img,
                "DNCM 快速 LUT" if dncm_mode == "fast" else "DNCM 高质量 LUT",
            )

            await prog("encode_output", 85, "编码输出图片...")
            await asyncio.sleep(0.01)

            elapsed = time.time() - start_time

            target_b64 = await asyncio.to_thread(_img_to_base64, target_img, ".jpg")
            reference_b64 = await asyncio.to_thread(_img_to_base64, reference_img, ".jpg")
            result_b64 = await asyncio.to_thread(_img_to_base64, result_img, ".png")
            project_result_path = ""
            project_result_url = ""
            if project_id > 0:
                project_result_path, project_result_url = await trace_to_thread(
                    "project_dncm_result_save",
                    _save_project_image,
                    project_id,
                    "result",
                    f"{session_id}_dncm_result.png",
                    result_img,
                    ".png",
                    [cv2.IMWRITE_PNG_COMPRESSION, 3],
                )

            await prog("done", 100, f"追色完成！耗时 {round(elapsed, 2)}s")
            await asyncio.sleep(0.01)

            mark_task_success()
            response_payload = {
                "success": True,
                "algorithm": algorithm,
                "algorithm_name": ALL_ALGORITHMS[algorithm]["name"],
                "processing_time": round(elapsed, 3),
                "session_id": session_id,
                "target_path": target_path,
                "reference_path": reference_path,
                "result_path": project_result_path,
                "lut_mode": dncm_mode,
                "lut_size": lut_size,
                "lut_path": lut_path,
                "reusable_preset": reusable_preset,
                "images": {
                    "target": f"data:image/jpeg;base64,{target_b64}",
                    "reference": f"data:image/jpeg;base64,{reference_b64}",
                    "result": f"data:image/png;base64,{result_b64}",
                    "result_url": project_result_url,
                },
            }
            if dncm_applied_mask_path:
                response_payload["mask"] = {
                    "mode": dncm_mask_mode,
                    "strength": float(mask_strength),
                    "mask_path": dncm_applied_mask_path,
                }
            if dncm_applied_depth_path:
                response_payload["depth"] = {
                    "strength": float(depth_strength),
                    "depth_path": dncm_applied_depth_path,
                }
            if dncm_semantic_meta:
                response_payload["semantic"] = {
                    "strength": float(semantic_strength),
                    "meta": dncm_semantic_meta,
                }
            return JSONResponse(response_payload)
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"DNCM LUT追色失败: {str(e)}")
    elif profile_file is not None and profile_file.filename:
        if profile_builtin:
            await prog("generate_lut", 25, f"生成{profile_builtin}预设 LUT...")
            await asyncio.sleep(0.01)
            lut_3d = await asyncio.to_thread(_generate_builtin_profile, profile_builtin)

            session_id = str(uuid.uuid4())
            session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
            os.makedirs(session_dir, exist_ok=True)
        else:
            await prog("parse_lut", 25, "解析 LUT 文件中...")
            await asyncio.sleep(0.01)

            lut_ext = os.path.splitext(profile_file.filename)[1].lower()
            lut_bytes = await profile_file.read()

            session_id = str(uuid.uuid4())
            session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
            os.makedirs(session_dir, exist_ok=True)

            import_lut_path = os.path.join(session_dir, f"imported_lut{lut_ext}")
            with open(import_lut_path, "wb") as f:
                f.write(lut_bytes)

            try:
                from core.io.lut_parser import parse_lut_file
                lut_3d = await asyncio.to_thread(parse_lut_file, import_lut_path)
            except Exception as e:
                raise_task_http_error(400, f"LUT 文件解析失败: {str(e)}")

        await asyncio.to_thread(np.save, os.path.join(session_dir, "lut_global.npy"), lut_3d)

        await prog("apply_lut", 50, "极速 LUT 渲染中...")
        await asyncio.sleep(0.01)

        from core.render.full_render import apply_lut
        target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
        result_rgb = await trace_to_thread("profile_apply_lut", apply_lut, target_rgb, lut_3d)
        result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)

        if reference_img is not None:
            try:
                from core.color.color_refine import refine_color_distribution
                result_img = await trace_to_thread(
                    "profile_color_refine",
                    refine_color_distribution, result_img, reference_img, 0.3
                )
                print("[Profile] Color refine applied with reference")
            except Exception as e:
                print(f"[Profile] Color refine failed: {e}")

    else:
        await prog("transfer", 30, f"色彩迁移中 ({ALL_ALGORITHMS[algorithm]['name']})...")
        await asyncio.sleep(0.01)
        kwargs = {}
        if algorithm == "luminance_partition":
            kwargs["blend_strength"] = blend_strength
        result_img = await trace_to_thread(
            "classic_transfer",
            transfer_color, target_img, reference_img, algorithm=algorithm, **kwargs
        )

        try:
            from core.color.color_refine import refine_color_distribution
            _cf_params = {
                "reinhard": (0.4, 0.0, 0.4, 0.3),
                "histogram": (0.3, 0.0, 0.3, 0.2),
                "luminance_partition": (0.5, 0.0, 0.5, 0.4),
            }
            _lm, _am, _bm, _ls = _cf_params.get(algorithm, (0.3, 0.0, 0.3, 0.2))
            result_img = await trace_to_thread(
                "classic_color_refine",
                refine_color_distribution,
                result_img, reference_img,
                l_mean_strength=_lm, a_mean_strength=_am, b_mean_strength=_bm,
                l_std_strength=_ls,
            )
            print(f"[Color Refine] Applied to {algorithm} (L_mean={_lm} a_mean={_am} b_mean={_bm} L_std={_ls})")
        except Exception as e:
            print(f"[Color Refine] {algorithm} refine failed: {e}, continuing")

    await prog("postprocess", 75, "智能处理中...")
    await asyncio.sleep(0.01)
    await wait_if_paused()

    trace_mark("postprocess_section_start")
    if enable_postprocess and algorithm not in ("regional_modflows", "regional_luminance", "ai_portrait"):
        cb = _make_thread_callback(task_id, 75, 85, loop) if has_progress else None
        try:
            result_img = await trace_to_thread(
                "smart_postprocess",
                enhance_transfer_result, result_img, target_img, reference_img,
                progress_callback=cb,
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(500, f"智能后处理失败: {str(e)}")

    semantic_meta = None
    if enable_semantic_match and reference_img is not None:
        if not model_runtime["semantic_match_enabled"]:
            raise_task_http_error(400, _disabled_model_error("dinov2_semantic_match"))
        await prog("semantic_match", 80, "应用参考图语义匹配...")
        await asyncio.sleep(0.01)
        try:
            result_img, semantic_meta = await trace_to_thread(
                "semantic_match_transfer",
                semantic_match_transfer,
                target_img,
                result_img,
                reference_img,
                semantic_strength,
            )
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(400, f"参考图语义匹配失败: {str(e)}")

    applied_depth_path = ""
    if enable_depth_layers and depth_path:
        if not model_runtime["depth_layers_enabled"]:
            raise_task_http_error(400, _disabled_model_error("depth_anything_v2"))
        await prog("depth_layers", 82, "应用深度分层追色...")
        await asyncio.sleep(0.01)
        try:
            depth_map = await trace_to_thread(
                "load_depth_layers",
                load_depth_file,
                depth_path,
                target_img.shape[:2],
            )
            if depth_map is None:
                raise ValueError("depth 文件不存在或无法读取")
            result_img = await trace_to_thread(
                "apply_depth_layers",
                apply_depth_layer_blend,
                target_img,
                result_img,
                depth_map,
                depth_strength,
            )
            applied_depth_path = depth_path
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(400, f"深度分层追色应用失败: {str(e)}")

    normalized_mask_mode = str(mask_mode or "none").lower()
    applied_mask_path = ""
    if normalized_mask_mode != "none" and mask_path:
        if not model_runtime["subject_mask_enabled"]:
            raise_task_http_error(400, _disabled_model_error("sam_subject_mask"))
        await prog("mask_blend", 84, "应用区域保护 mask...")
        await asyncio.sleep(0.01)
        try:
            subject_mask = await trace_to_thread(
                "load_subject_mask",
                load_mask_file,
                mask_path,
                target_img.shape[:2],
            )
            if subject_mask is None:
                raise ValueError("mask 文件不存在或无法读取")
            result_img = await trace_to_thread(
                "apply_subject_mask",
                apply_subject_mask_blend,
                target_img,
                result_img,
                subject_mask,
                normalized_mask_mode,
                mask_strength,
            )
            applied_mask_path = mask_path
        except Exception as e:
            await prog("error", 0, str(e))
            raise_task_http_error(400, f"区域保护 mask 应用失败: {str(e)}")

    await prog("analyze_style", 86, "分析风格信息用于全尺寸导出...")
    await asyncio.sleep(0.01)

    trace_mark("postprocess_section_done")
    if not ((profile_file is not None and profile_file.filename) or profile_builtin):
        try:
            from core.cache import StyleRepresentation, get_cache_manager
            from core.render import analyze_style_stats
            color_stats = await trace_to_thread("analyze_style_stats", analyze_style_stats, target_img, reference_img)
            style = StyleRepresentation(
                color_stats=color_stats,
                algorithm_name=algorithm,
                blend_strength=blend_strength,
                smart_postprocess=enable_postprocess,
                created_at=time.time()
            )
            cache_mgr = get_cache_manager()
            cache_mgr.save_style(target_path, style, algorithm)
        except Exception:
            pass
    trace_mark("analyze_style_section_done")

    if not ((profile_file is not None and profile_file.filename) or profile_builtin):
        await prog("extract_lut", 88, "提取 3D LUT 用于全尺寸渲染...")
        await asyncio.sleep(0.01)

    if algorithm != "ai_portrait":
        session_id = str(uuid.uuid4())

    if applied_depth_path:
        try:
            session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
            os.makedirs(session_dir, exist_ok=True)
            session_depth_path = os.path.join(session_dir, "depth_layers.png")
            await trace_to_thread("copy_depth_layers", shutil.copyfile, applied_depth_path, session_depth_path)
            await trace_to_thread(
                "write_depth_layers_meta",
                _write_session_depth_meta,
                session_dir,
                depth_strength,
                applied_depth_path,
            )
        except Exception as exc:
            print(f"[DepthLayers] cache copy failed: {exc}")

    if applied_mask_path:
        try:
            session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
            os.makedirs(session_dir, exist_ok=True)
            session_mask_path = os.path.join(session_dir, "subject_mask.png")
            await trace_to_thread("copy_subject_mask", shutil.copyfile, applied_mask_path, session_mask_path)
            await trace_to_thread(
                "write_subject_mask_meta",
                _write_session_subject_mask_meta,
                session_dir,
                normalized_mask_mode,
                mask_strength,
                applied_mask_path,
            )
        except Exception as exc:
            print(f"[SubjectMask] cache copy failed: {exc}")

    trace_mark("lut_section_start", session_id=session_id)
    try:
        from core.color.lut_extractor import extract_lut_from_pair

        if algorithm == "ai_portrait":
            trace_mark("lut_prepare_rgb_start")
            target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            result_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            trace_mark("lut_prepare_rgb_done")
            lut_3d = await trace_to_thread("extract_lut_ai_portrait", extract_lut_from_pair, target_rgb, result_rgb, 33)
            lut_path = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}.npy")
            await trace_to_thread("save_lut_ai_portrait", np.save, lut_path, lut_3d)
        else:
            trace_mark("lut_prepare_rgb_start")
            target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            result_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            trace_mark("lut_prepare_rgb_done")
            lut_3d = await trace_to_thread("extract_lut", extract_lut_from_pair, target_rgb, result_rgb, 33)
            lut_path = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}.npy")
            await trace_to_thread("save_lut", np.save, lut_path, lut_3d)
    except Exception as e:
        trace_mark("lut_pipeline_error", error=type(e).__name__)
        print(f"[LUT] extract failed: {e}")
    trace_mark("lut_section_done", session_id=session_id)

    project_result_path = ""
    project_result_url = ""
    try:
        def _display_preview(img, max_edge=2048):
            h, w = img.shape[:2]
            longest = max(w, h)
            if longest <= max_edge:
                return img
            scale = max_edge / float(longest)
            return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        preview_path = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}_result_preview.jpg")
        trace_mark("preview_result_encode_start")
        cv2.imencode('.jpg', _display_preview(result_img), [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(preview_path)
        trace_mark("preview_result_encode_done")
        orig_preview_path = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}_orig_preview.jpg")
        trace_mark("preview_target_encode_start")
        cv2.imencode('.jpg', _display_preview(target_img), [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(orig_preview_path)
        trace_mark("preview_target_encode_done")
        if project_id > 0:
            project_result_path, project_result_url = await trace_to_thread(
                "project_result_preview_save",
                _save_project_image,
                project_id,
                "result",
                f"{session_id}_result_preview.jpg",
                _display_preview(result_img),
                ".jpg",
                [cv2.IMWRITE_JPEG_QUALITY, 90],
            )
    except Exception as e:
        trace_mark("preview_encode_error", error=type(e).__name__)
        print(f"[Cache] Failed to save result preview: {e}")

    await prog("encode_output", 92, "编码输出图片...")
    await asyncio.sleep(0.01)

    trace_mark("preview_written", session_id=session_id)
    elapsed = time.time() - start_time

    metrics = None
    if enable_metrics and reference_img is not None:
        await prog("metrics", 96, "计算质量评估指标...")
        await asyncio.sleep(0.01)
        try:
            trace_mark("metrics_prepare_rgb_start")
            result_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            reference_rgb = cv2.cvtColor(reference_img, cv2.COLOR_BGR2RGB)
            target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            trace_mark("metrics_prepare_rgb_done")
            metrics = await trace_to_thread("evaluate_transfer", evaluate_transfer, result_rgb, reference_rgb, target_rgb)
        except Exception as e:
            trace_mark("metrics_error", error=type(e).__name__)
            metrics = {"error": str(e)}
    trace_mark("metrics_done", has_metrics=int(bool(metrics)))

    await prog("done", 100, f"追色完成！耗时 {round(elapsed, 2)}s")
    await asyncio.sleep(0.01)

    trace_mark("sse_done_sent", elapsed_s=round(elapsed, 3))
    mark_task_success()
    trace_mark("task_log_done")
    preview_token = int(time.time() * 1000)
    result_preview_url = project_result_url or f"/temp_luts/{session_id}_result_preview.jpg?t={preview_token}"
    target_preview_url = f"/temp_luts/{session_id}_orig_preview.jpg?t={preview_token}"
    response_images = {
        "target_url": target_preview_url,
        "reference_url": "",
        "result_url": result_preview_url,
    }
    if generate_lut_only:
        response_images.update({
            "target": target_preview_url,
            "reference": "",
            "result": result_preview_url,
        })
    else:
        target_b64 = await asyncio.to_thread(_img_to_base64, target_img, ".jpg")
        reference_b64 = await asyncio.to_thread(_img_to_base64, reference_img, ".jpg") if reference_img is not None else ""
        result_b64 = await asyncio.to_thread(_img_to_base64, result_img, ".png")
        response_images.update({
            "target": f"data:image/jpeg;base64,{target_b64}",
            "reference": f"data:image/jpeg;base64,{reference_b64}" if reference_b64 else "",
            "result": f"data:image/png;base64,{result_b64}",
        })
    trace_mark("response_images_ready")

    response = {
        "success": True,
        "algorithm": algorithm,
        "algorithm_name": ALL_ALGORITHMS[algorithm]["name"],
        "processing_time": round(elapsed, 3),
        "session_id": session_id,
        "target_path": target_path,
        "reference_path": reference_path,
        "result_path": project_result_path,
        "images": response_images,
    }
    if applied_mask_path:
        response["mask"] = {
            "mode": normalized_mask_mode,
            "strength": float(mask_strength),
            "mask_path": applied_mask_path,
        }
    if applied_depth_path:
        response["depth"] = {
            "strength": float(depth_strength),
            "depth_path": applied_depth_path,
        }
    if semantic_meta:
        response["semantic"] = {
            "strength": float(semantic_strength),
            "meta": semantic_meta,
        }

    if metrics:
        response["metrics"] = metrics

    trace_mark("json_response_return")
    return JSONResponse(response)


@app.post("/api/video_transfer")
async def api_video_transfer(
    video: UploadFile = File(None),
    video_path: str = Form(None),
    reference: UploadFile = File(None),
    algorithm: str = Form("luminance_partition"),
    blend_strength: float = Form(0.85),
    enable_postprocess: bool = Form(True),
    task_id: str = Form(None),
    key_frame_interval: int = Form(30),
    transition_frames: int = Form(5),
    enable_scene_detect: bool = Form(True),
    custom_keyframes: str = Form(None),
    captured_style_id: str = Form(None),
    profile_file: UploadFile = File(None),
    profile_builtin: str = Form(None),
    project_id: int = Form(0),
    authorization: Optional[str] = Header(None),
):
    model_runtime = _resolve_transfer_model_runtime(algorithm)
    algorithm = model_runtime["algorithm"]
    disabled_model = model_runtime.get("model_key")
    if disabled_model and disabled_model in model_runtime["disabled"]:
        raise HTTPException(status_code=400, detail=_disabled_model_error(disabled_model))

    if algorithm not in ALL_ALGORITHMS:
        raise HTTPException(status_code=400, detail=f"不支持的算法: {algorithm}")

    request_user_id = _get_request_user_id(authorization)
    request_user_role = _get_request_user_role(authorization)
    project_id = await _ensure_project_access(project_id, request_user_id)
    if request_user_id is not None:
        record_user_usage(request_user_id)

    if task_id is None:
        task_id = uuid.uuid4().hex
    pm = progress_manager
    pm.register_task(task_id)

    resolved_video_path = _resolve_local_file_path(video_path)
    if resolved_video_path and resolved_video_path.exists():
        video_path = str(resolved_video_path)
    elif video is not None and video.filename:
        ensure_upload_file_size(video, 300 * 1024 * 1024, label="视频文件")
        video_path = _save_upload(video, project_id=project_id, bucket="video_source")
    else:
        raise HTTPException(status_code=400, detail="请提供视频文件")

    resolved_reference_path = _resolve_local_file_path(reference_path)
    if resolved_reference_path and resolved_reference_path.exists():
        reference_path = str(resolved_reference_path)
    elif reference is not None and reference.filename:
        ensure_upload_file_size(reference, 10 * 1024 * 1024, label="参考图")
        reference_path = _save_upload(reference, project_id=project_id, bucket="video_reference")
    else:
        reference_path = None

    profile_path = None
    if profile_file is not None and profile_file.filename:
        ensure_upload_file_size(profile_file, 10 * 1024 * 1024, label="风格文件")
        profile_ext = os.path.splitext(profile_file.filename)[1].lower()
        profile_name = f"profile_{uuid.uuid4().hex}{profile_ext}"
        if project_id > 0:
            profile_path = str(_safe_project_bucket_dir(project_id, "video_profile") / profile_name)
        else:
            profile_path = os.path.join(str(_runtime_upload_dir()), profile_name)
        profile_content = await profile_file.read()
        with open(profile_path, "wb") as f:
            f.write(profile_content)

    asyncio.create_task(_background_video_transfer(
        video_path, reference_path, algorithm, blend_strength,
        enable_postprocess, task_id, key_frame_interval,
        transition_frames, profile_path, profile_builtin,
        enable_scene_detect, custom_keyframes,
        captured_style_id,
        request_user_id,
        request_user_role,
        model_runtime,
        project_id,
    ))

    _write_task_log(
        task_id=task_id,
        task_type="视频追色",
        event_type="request",
        status="info",
        summary="视频追色任务已启动",
        detail=ALL_ALGORITHMS.get(algorithm, {}).get("name", algorithm),
        user_id=request_user_id,
        role=request_user_role,
        model=algorithm,
        meta={"enable_postprocess": enable_postprocess, "enable_scene_detect": enable_scene_detect, "project_id": project_id},
    )

    return JSONResponse({
        "success": True,
        "task_id": task_id,
        "message": "任务已提交，正在后台处理",
    })


def _save_frame(ext, result_frame, frame_path):
    cv2.imencode(ext, result_frame)[1].tofile(frame_path)


async def _background_video_transfer(
    video_path, reference_path, algorithm, blend_strength,
    enable_postprocess, task_id, key_frame_interval,
    transition_frames, profile_path, profile_builtin,
    enable_scene_detect=False, custom_keyframes=None,
    captured_style_id=None,
    request_user_id=None, user_role="",
    model_runtime=None,
    project_id=0,
):
    pm = progress_manager
    frames_dir = None

    async def prog(stage, progress, message=""):
        elapsed = time.time() - _prog_start
        await pm.send(task_id, stage, progress, message, elapsed=round(elapsed, 1))

    recorded_model_call = False

    def mark_video_model_call(name):
        nonlocal recorded_model_call
        if recorded_model_call:
            return
        record_model_call(name)
        recorded_model_call = True

    recorded_task_outcome = False

    def mark_video_task_success():
        nonlocal recorded_task_outcome
        if recorded_task_outcome:
            return
        record_task_outcome(True)
        _write_task_log(
            task_id=task_id,
            task_type="视频追色",
            event_type="result",
            status="ok",
            summary="视频追色完成",
            detail=ALL_ALGORITHMS.get(algorithm, {}).get("name", algorithm),
            user_id=request_user_id,
            role=user_role,
            model=algorithm,
            duration_ms=_task_elapsed_ms(_prog_start),
        )
        recorded_task_outcome = True

    def mark_video_task_failure():
        nonlocal recorded_task_outcome
        if recorded_task_outcome:
            return
        record_task_outcome(False)
        _write_task_log(
            task_id=task_id,
            task_type="视频追色",
            event_type="result",
            status="fail",
            summary="视频追色失败",
            detail=ALL_ALGORITHMS.get(algorithm, {}).get("name", algorithm),
            user_id=request_user_id,
            role=user_role,
            model=algorithm,
            duration_ms=_task_elapsed_ms(_prog_start),
        )
        recorded_task_outcome = True

    loop = asyncio.get_event_loop()
    _prog_start = time.time()
    model_runtime = model_runtime or _resolve_transfer_model_runtime(algorithm)
    algorithm = model_runtime["algorithm"]

    import numpy as np

    try:
        reference_img = None
        if reference_path is not None:
            reference_img = await asyncio.to_thread(_cv2_imread_full, reference_path)
        if reference_img is None and profile_path is None and not profile_builtin and not captured_style_id:
            await prog("error", 0, "无法读取参考图片")
            mark_video_task_failure()
            return

        await prog("upload", 15, "文件已加载，开始提取帧...")
        await asyncio.sleep(0.01)

        output_filename = f"{uuid.uuid4().hex}_result.mp4"
        if _normalize_project_id(project_id) > 0:
            output_path = str(_safe_project_bucket_dir(project_id, "video_results") / output_filename)
            result_url = f"/api/project_assets/{_normalize_project_id(project_id)}/video_results/{output_filename}"
        else:
            output_path = os.path.join(str(_runtime_video_dir()), output_filename)
            result_url = f"/videos/{output_filename}"

        from algorithms.video.processor import extract_frames, assemble_video
        from algorithms.postprocess import regional_transfer, enhance_transfer_result

        await prog("extract", 20, "提取视频帧中...")
        await asyncio.sleep(0.01)

        frames_info = await asyncio.to_thread(extract_frames, video_path)
        frames_dir = frames_info["output_dir"]
        fps = frames_info["fps"]

        await prog("extract", 25, "帧提取完成，开始逐帧追色...")
        await asyncio.sleep(0.01)

        await pm.send(task_id, "metadata", 25, "视频元数据",
                      elapsed=round(time.time() - _prog_start, 1),
                      video_fps=round(fps, 2),
                      video_width=frames_info["width"],
                      video_height=frames_info["height"])

        frame_files = sorted([f for f in os.listdir(frames_dir) if f.endswith(('.png', '.jpg', '.jpeg'))])
        total_frames = len(frame_files)

        if total_frames == 0:
            await prog("error", 0, "无法提取视频帧")
            mark_video_task_failure()
            return

        avg_diff = 0

        if captured_style_id is not None:
            style_lut_path = os.path.join(str(STYLES_EXTRACTED_DIR), captured_style_id, "lut_global.npy")
            if not os.path.exists(style_lut_path):
                await prog("error", 0, f"风格LUT文件不存在: {captured_style_id}")
                mark_video_task_failure()
                return
            await prog("load_style", 26, f"加载相机风格 LUT...")
            await asyncio.sleep(0.01)
            from core.render.full_render import apply_lut
            video_lut = np.load(style_lut_path)
            if video_lut.dtype == np.float64:
                video_lut = video_lut.astype(np.float32)

            for i, frame_file in enumerate(frame_files):
                if pm.is_cancelled(task_id):
                    await prog("cancelled", 0, "任务已取消")
                    return
                while pm.is_paused(task_id):
                    await asyncio.sleep(0.5)
                frame_path = os.path.join(frames_dir, frame_file)
                frame_img = await asyncio.to_thread(_cv2_imread_full, frame_path)
                if frame_img is None:
                    continue
                pct = 25 + int((i + 1) / total_frames * 65)
                await prog("process", pct, f"LUT 极速渲染帧 {i+1}/{total_frames}...")
                frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                result_rgb = await asyncio.to_thread(apply_lut, frame_rgb, video_lut)
                result_frame = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
                ext = os.path.splitext(frame_path)[1]
                await asyncio.to_thread(_save_frame, ext, result_frame, frame_path)
            del video_lut

        elif profile_path is not None:
            if profile_builtin:
                await prog("generate_lut", 26, f"生成{profile_builtin}预设 LUT...")
                await asyncio.sleep(0.01)
                from core.render.full_render import apply_lut
                video_lut = await asyncio.to_thread(_generate_builtin_profile, profile_builtin)
            else:
                await prog("parse_lut", 26, "解析 LUT 文件...")
                await asyncio.sleep(0.01)
                from core.render.full_render import apply_lut
                lut_bytes = open(profile_path, "rb").read()
                _tmp_lut_path = os.path.join(frames_dir, f"_imported_lut{os.path.splitext(profile_path)[1]}")
                with open(_tmp_lut_path, "wb") as f:
                    f.write(lut_bytes)
                from core.io.lut_parser import parse_lut_file
                video_lut = await asyncio.to_thread(parse_lut_file, _tmp_lut_path)

            for i, frame_file in enumerate(frame_files):
                if pm.is_cancelled(task_id):
                    await prog("cancelled", 0, "任务已取消")
                    return
                while pm.is_paused(task_id):
                    await asyncio.sleep(0.5)
                frame_path = os.path.join(frames_dir, frame_file)
                frame_img = await asyncio.to_thread(_cv2_imread_full, frame_path)
                if frame_img is None:
                    continue
                pct = 25 + int((i + 1) / total_frames * 65)
                await prog("process", pct, f"LUT 极速渲染帧 {i+1}/{total_frames}...")
                frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                result_rgb = await asyncio.to_thread(apply_lut, frame_rgb, video_lut)
                result_frame = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
                ext = os.path.splitext(frame_path)[1]
                await asyncio.to_thread(_save_frame, ext, result_frame, frame_path)
            del video_lut

        elif profile_builtin:
            await prog("generate_lut", 26, f"生成{profile_builtin}预设 LUT...")
            await asyncio.sleep(0.01)
            from core.render.full_render import apply_lut
            video_lut = await asyncio.to_thread(_generate_builtin_profile, profile_builtin)

            for i, frame_file in enumerate(frame_files):
                if pm.is_cancelled(task_id):
                    await prog("cancelled", 0, "任务已取消")
                    return
                while pm.is_paused(task_id):
                    await asyncio.sleep(0.5)
                frame_path = os.path.join(frames_dir, frame_file)
                frame_img = await asyncio.to_thread(_cv2_imread_full, frame_path)
                if frame_img is None:
                    continue
                pct = 25 + int((i + 1) / total_frames * 65)
                await prog("process", pct, f"LUT 极速渲染帧 {i+1}/{total_frames}...")
                frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                result_rgb = await asyncio.to_thread(apply_lut, frame_rgb, video_lut)
                result_frame = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
                ext = os.path.splitext(frame_path)[1]
                await asyncio.to_thread(_save_frame, ext, result_frame, frame_path)
            del video_lut

        else:
            from algorithms.video.processor import is_key_frame
            from core.color.lut_extractor import extract_lut_from_pair
            from core.render.full_render import apply_lut

            current_lut = None
            target_lut = None
            transition_count = 0
            prev_frame = None
            hist_cache = {}
            custom_kf_set = None
            if custom_keyframes:
                try:
                    custom_kf_set = set(int(x.strip()) for x in custom_keyframes.split(',') if x.strip())
                except (ValueError, TypeError):
                    custom_kf_set = None

            for i, frame_file in enumerate(frame_files):
                if pm.is_cancelled(task_id):
                    await prog("cancelled", 0, "任务已取消")
                    return
                while pm.is_paused(task_id):
                    await asyncio.sleep(0.5)
                frame_path = os.path.join(frames_dir, frame_file)
                frame_img = await asyncio.to_thread(_cv2_imread_full, frame_path)
                if frame_img is None:
                    continue
                key_type = is_key_frame(prev_frame, frame_img, i, interval=key_frame_interval, cache=hist_cache, custom_keyframes=custom_kf_set)
                if not enable_scene_detect and key_type == "scene":
                    key_type = None
                pct = 25 + int((i + 1) / total_frames * 65)

                if key_type is not None:
                    if i == total_frames - 1:
                        await prog("process", pct, f"末帧融合追色 {i+1}/{total_frames}...")
                        if current_lut is not None:
                            frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                            result_rgb = await asyncio.to_thread(apply_lut, frame_rgb, current_lut)
                            result_frame = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
                        else:
                            result_frame = frame_img
                        ext = os.path.splitext(frame_path)[1]
                        await asyncio.to_thread(_save_frame, ext, result_frame, frame_path)
                        prev_frame = frame_img
                        continue
                    else:
                        key_type_label = {"first": "首帧", "interval": "间隔", "scene": "场景切换"}.get(key_type, "关键帧")
                        await prog("process", pct, f"分析{key_type_label} {i+1}/{total_frames}...")

                        if algorithm == "neural_preset":
                            if "neural_preset" in model_runtime["disabled"]:
                                raise RuntimeError(_disabled_model_error("neural_preset"))
                            mark_video_model_call("neural_preset")
                            result_frame = await asyncio.to_thread(
                                neural_preset_transfer, frame_img, reference_img, model_dir=str(NEURALPRESET_MODEL_DIR))
                        elif algorithm == "modflows":
                            if not model_runtime["modflows_enabled"]:
                                raise RuntimeError(_disabled_model_error(model_runtime["modflows_key"]))
                            mark_video_model_call(model_runtime["modflows_key"])
                            result_frame = await asyncio.to_thread(
                                modflows_transfer, frame_img, reference_img,
                                encoder_type=model_runtime["modflows_encoder"], steps=16, strength=1.0)
                        elif algorithm == "modflows_b0":
                            if not model_runtime["modflows_b0_enabled"]:
                                raise RuntimeError(_disabled_model_error("modflows_b0"))
                            mark_video_model_call("modflows_b0")
                            result_frame = await asyncio.to_thread(
                                modflows_transfer, frame_img, reference_img,
                                encoder_type="B0", steps=6, strength=0.75)
                        elif algorithm == "regional_modflows":
                            if not model_runtime["modflows_enabled"]:
                                raise RuntimeError(_disabled_model_error(model_runtime["modflows_key"]))
                            mark_video_model_call(model_runtime["modflows_key"])
                            result_frame = await asyncio.to_thread(
                                regional_transfer, frame_img, reference_img,
                                transfer_func=lambda t, r: modflows_transfer(t, r, encoder_type=model_runtime["modflows_encoder"], steps=16, strength=1.0),
                                base_strength=blend_strength)
                        elif algorithm == "regional_luminance":
                            result_frame = await asyncio.to_thread(
                                regional_transfer, frame_img, reference_img,
                                transfer_func=lambda t, r: transfer_color(t, r, algorithm="luminance_partition", blend_strength=1.0),
                                base_strength=blend_strength)
                        else:
                            result_frame = await asyncio.to_thread(
                                transfer_color, frame_img, reference_img, algorithm=algorithm, blend_strength=blend_strength)

                        if enable_postprocess and algorithm not in ("regional_modflows", "regional_luminance"):
                            result_frame = await asyncio.to_thread(
                                enhance_transfer_result, result_frame, frame_img, reference_img)

                        try:
                            src_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                            tgt_rgb = cv2.cvtColor(result_frame, cv2.COLOR_BGR2RGB)
                            new_lut = await asyncio.to_thread(extract_lut_from_pair, src_rgb, tgt_rgb, 33)
                        except Exception:
                            new_lut = None

                        if key_type in ("first", "scene") or current_lut is None:
                            current_lut = new_lut if new_lut is not None else current_lut
                            target_lut = None
                            transition_count = transition_frames
                        else:
                            if new_lut is not None and current_lut is not None:
                                target_lut = new_lut
                                transition_count = 0
                            else:
                                current_lut = new_lut if new_lut is not None else current_lut
                                target_lut = None
                                transition_count = transition_frames

                if key_type is None:
                    if target_lut is not None and current_lut is not None and transition_count < transition_frames:
                        await prog("process", pct, f"过渡渲染帧 {i+1}/{total_frames}...")
                        transition_count += 1
                        alpha = min(transition_count / transition_frames, 1.0)
                        mixed_lut = current_lut * (1.0 - alpha) + target_lut * alpha
                        frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                        result_rgb = await asyncio.to_thread(apply_lut, frame_rgb, mixed_lut.astype(np.float32))
                        result_frame = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
                        if alpha >= 1.0:
                            current_lut = target_lut
                            target_lut = None
                    elif current_lut is not None:
                        await prog("process", pct, f"渲染帧 {i+1}/{total_frames}...")
                        frame_rgb = cv2.cvtColor(frame_img, cv2.COLOR_BGR2RGB)
                        result_rgb = await asyncio.to_thread(apply_lut, frame_rgb, current_lut)
                        result_frame = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
                    else:
                        result_frame = frame_img

                ext = os.path.splitext(frame_path)[1]
                await asyncio.to_thread(_save_frame, ext, result_frame, frame_path)
                prev_frame = frame_img

            # 诊断：采样帧计算平均像素差异
            try:
                import cv2 as cv2_diag
                cap_diag = cv2_diag.VideoCapture(video_path)
                sample_indices = [0, total_frames // 2, total_frames - 2]
                diffs = []
                for idx in sample_indices:
                    if 0 <= idx < total_frames:
                        cap_diag.set(cv2_diag.CAP_PROP_POS_FRAMES, idx)
                        ret, orig = cap_diag.read()
                        if ret and orig is not None:
                            result = await asyncio.to_thread(_cv2_imread_full, os.path.join(frames_dir, frame_files[idx]))
                            if result is not None:
                                diff = float(np.mean(np.abs(orig.astype(float) - result.astype(float))))
                                diffs.append(diff)
                cap_diag.release()
                avg_diff = round(float(np.mean(diffs)), 2) if diffs else 0
            except Exception:
                avg_diff = 0

            await prog("assemble", 95, "正在合成视频...")
            await asyncio.sleep(0.01)

        await asyncio.to_thread(assemble_video, frames_dir, output_path, fps=fps, audio_source=video_path)
        import shutil
        shutil.rmtree(frames_dir, ignore_errors=True)

        done_msg = f"视频追色完成！平均像素差异: {avg_diff}" if avg_diff > 0 else "视频追色完成！"
        await pm.send(task_id, "done", 100, done_msg, elapsed=round(time.time() - _prog_start, 1), result_url=result_url, avg_diff=avg_diff)
        mark_video_task_success()

    except Exception as e:
        import shutil
        try:
            if frames_dir:
                shutil.rmtree(frames_dir, ignore_errors=True)
        except:
            pass
        mark_video_task_failure()
        await prog("error", 0, str(e))



@app.post("/api/export_video")
async def api_export_video(
    source_url: str = Form(...),
    format: str = Form("mp4"),
    bitrate: str = Form("8"),
    resolution: str = Form("original"),
    fps: str = Form("original"),
    project_id: int = Form(0),
    authorization: Optional[str] = Header(None),
):
    request_user_id = _get_request_user_id(authorization)
    request_user_role = _get_request_user_role(authorization)
    project_id = await _ensure_project_access(project_id, request_user_id)
    source_resolved = _resolve_local_file_path(source_url)
    source_path = str(source_resolved) if source_resolved else os.path.join(str(_runtime_video_dir()), os.path.basename(source_url))
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="源视频文件不存在")

    from algorithms.video.processor import _get_ffmpeg_path
    ffmpeg = _get_ffmpeg_path()

    ext_map = {"mp4": ".mp4", "mp4_h265": ".mp4", "mov": ".mov", "avi": ".avi"}
    export_filename = f"export_{uuid.uuid4().hex}{ext_map.get(format, '.mp4')}"
    if project_id > 0:
        export_path = str(_safe_project_bucket_dir(project_id, "video_exports") / export_filename)
    else:
        export_path = os.path.join(str(_runtime_video_dir()), export_filename)

    cmd = [ffmpeg, "-y", "-i", source_path]

    codec_map = {
        "mp4": "libx264",
        "mp4_h265": "libx265",
        "mov": "prores_ks",
        "avi": "rawvideo",
    }
    vcodec = codec_map.get(format, "libx264")

    cmd += ["-c:v", vcodec]

    if format in ("mp4", "mp4_h265"):
        cmd += ["-b:v", f"{bitrate}M"]

    if resolution != "original":
        res_map = {"4k": "3840:2160", "1080p": "1920:1080", "720p": "1280:720", "480p": "854:480"}
        if resolution in res_map:
            cmd += ["-vf", f"scale={res_map[resolution]}"]

    if fps != "original":
        cmd += ["-r", str(fps)]

    if format == "avi":
        cmd += ["-pix_fmt", "bgr24"]
    elif format == "mov":
        cmd += ["-pix_fmt", "yuv422p10le", "-profile:v", "3"]
    else:
        cmd += ["-pix_fmt", "yuv420p"]

    if format == "mov":
        cmd += ["-c:a", "pcm_s16le"]
    elif format == "avi":
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd.append(export_path)

    import subprocess
    try:
        subprocess.run(cmd, check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        if os.path.exists(export_path):
            os.remove(export_path)
        raise HTTPException(status_code=500, detail=f"导出转码失败: {str(e)}")

    from fastapi.responses import FileResponse
    record_export(1)
    export_size_bytes = os.path.getsize(export_path) if os.path.exists(export_path) else 0
    if request_user_id is not None:
        record_user_usage(request_user_id)
    _write_task_log(
        task_id="",
        task_type="导出",
        event_type="result",
        status="ok",
        summary="视频导出完成",
        detail=f"格式: {format}",
        user_id=request_user_id,
        role=request_user_role,
        model=format,
        meta={
            "bitrate": bitrate,
            "resolution": resolution,
            "fps": fps,
            "export_format": format,
            "export_size_bytes": export_size_bytes,
            "export_path": export_path,
            "project_id": project_id,
        },
    )
    return FileResponse(
        export_path,
        media_type="video/mp4" if format in ("mp4","mp4_h265") else f"video/{format}",
        filename=f"ColorChase_export.{'mov' if format == 'mov' else 'avi' if format == 'avi' else 'mp4'}",
    )


@app.post("/api/preview_upload")
async def api_preview_upload(file: UploadFile = File(...)):
    ensure_upload_file_size(file, 10 * 1024 * 1024, label="预览文件")
    ext = os.path.splitext(file.filename)[1].lower() if file.filename else ".jpg"
    filename = f"preview_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(str(_runtime_upload_dir()), filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    raw_exts = {'.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2', '.raf', '.rw2', '.raw', '.rwl', '.orf', '.pef', '.ptx', '.3fr', '.fff', '.iiq', '.cap', '.eip', '.mef', '.mos', '.mfw', '.x3f', '.dcr', '.kdc', '.k25', '.dcs', '.srw', '.erf', '.cs1', '.cs4', '.cs16', '.sti', '.bay', '.pxn', '.braw', '.r3d', '.ari', '.cine', '.lfp', '.rwz'}
    if ext in raw_exts:
        preview_path = filepath + ".jpg"
        preview_ok = False

        try:
            import rawpy
            import tempfile
            import shutil
            try:
                with rawpy.imread(filepath) as raw:
                    rgb = raw.postprocess(
                        half_size=True,
                        output_bps=8,
                        use_camera_wb=True,
                        no_auto_bright=True,
                        output_color=rawpy.ColorSpace.sRGB,
                        gamma=(2.2, 4.5),
                    )
            except Exception as path_err:
                print(f"[Preview] rawpy direct read failed (likely CJK path): {path_err}")
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_raw = os.path.join(tmp_dir, "raw_temp" + ext)
                    shutil.copy2(filepath, tmp_raw)
                    with rawpy.imread(tmp_raw) as raw:
                        rgb = raw.postprocess(
                            half_size=True,
                            output_bps=8,
                            use_camera_wb=True,
                            no_auto_bright=True,
                            output_color=rawpy.ColorSpace.sRGB,
                            gamma=(2.2, 4.5),
                        )
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                buf.tofile(preview_path)
            preview_ok = True
        except Exception as e:
            print(f"[Preview] rawpy decode failed: {e}")

        if not preview_ok:
            try:
                import imageio
                rgb = imageio.v3.imread(filepath)
                if rgb is not None:
                    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                    max_dim = 2048
                    h, w = bgr.shape[:2]
                    if max(h, w) > max_dim:
                        scale = max_dim / max(h, w)
                        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        buf.tofile(preview_path)
                    preview_ok = True
            except Exception as e:
                print(f"[Preview] imageio fallback failed: {e}")

        if not preview_ok:
            try:
                from PIL import Image
                img = Image.open(filepath)
                if hasattr(img, 'thumbnail'):
                    max_preview = 2048
                    img.thumbnail((max_preview, max_preview), Image.LANCZOS)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                img.save(preview_path, "JPEG", quality=85)
                preview_ok = True
            except Exception as e:
                print(f"[Preview] PIL fallback failed: {e}")

        if not preview_ok:
            try:
                placeholder = np.zeros((400, 600, 3), dtype=np.uint8)
                cv2.rectangle(placeholder, (0, 0), (600, 400), (40, 40, 40), -1)
                font = cv2.FONT_HERSHEY_SIMPLEX
                lines = [
                    "RAW Preview Unavailable",
                    "Color transfer still works!",
                    f"Format: {ext.upper()}",
                ]
                y_start = 140
                for i, line in enumerate(lines):
                    scale = 0.8 if i == 0 else 0.6
                    color = (200, 200, 200) if i == 0 else (150, 150, 150)
                    (tw, th), _ = cv2.getTextSize(line, font, scale, 1)
                    x = (600 - tw) // 2
                    cv2.putText(placeholder, line, (x, y_start + i * 50), font, scale, color, 1, cv2.LINE_AA)
                cv2.imencode('.jpg', placeholder, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(preview_path)
                preview_ok = True
            except Exception as e:
                print(f"[Preview] Placeholder generation failed: {e}")

        if preview_ok and os.path.exists(preview_path):
            return FileResponse(preview_path, media_type="image/jpeg")

        return JSONResponse(
            {"preview_available": False, "message": "RAW预览不可用，但追色功能正常"},
            status_code=200,
        )

    return FileResponse(filepath, media_type="image/jpeg" if ext in ('.jpg', '.jpeg') else "image/png")


@app.post("/api/download_full")
async def api_download_full(
    target_path: str = Form(...),
    session_id: str = Form(None),
    format: str = Form("png"),
    task_id: str = Form(None),
    intensity: float = Form(100.0),
    exposure: float = Form(100.0),
    contrast: float = Form(100.0),
    highlight: float = Form(100.0),
    shadow: float = Form(100.0),
    vibrance: float = Form(100.0),
    size_mode: str = Form("full"),
    merged_session_id: str = Form(None),
    authorization: Optional[str] = Header(None),
):
    import gc

    pm = progress_manager
    has_progress = task_id is not None
    request_user_id = _get_request_user_id(authorization)
    request_user_role = _get_request_user_role(authorization)
    recorded_export_event = False
    export_size_bytes = 0
    if has_progress:
        pm.register_task(task_id)

    async def prog(stage, progress, message=""):
        if has_progress:
            await pm.send(task_id, stage, progress, message)

    def mark_download_export():
        nonlocal recorded_export_event
        if recorded_export_event:
            return
        record_export(1)
        if request_user_id is not None:
            record_user_usage(request_user_id)
        _write_task_log(
            task_id=task_id or "",
            task_type="导出",
            event_type="result",
            status="ok",
            summary="图片导出完成",
            detail=size_mode,
            user_id=request_user_id,
            role=_get_request_user_role(authorization),
            model=size_mode,
            meta={
                "export_format": format,
                "size_mode": size_mode,
                "export_size_bytes": export_size_bytes,
            },
        )
        recorded_export_event = True

    if not session_id and not merged_session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id，请先预览追色")

    has_merged = bool(merged_session_id)
    merged_lut_path = os.path.join(str(_runtime_temp_lut_dir()), f"{merged_session_id}.npy") if has_merged else None

    session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id) if session_id else None
    lut_path = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}.npy") if session_id else None
    has_portrait_cache = (
        session_id and os.path.isdir(session_dir)
        and os.path.exists(os.path.join(session_dir, "lut_global.npy"))
        and os.path.exists(os.path.join(session_dir, "mask_soft.png"))
    )
    has_lut_cache = (
        session_id and os.path.isdir(session_dir)
        and os.path.exists(os.path.join(session_dir, "lut_global.npy"))
    )

    ref_stats = None
    if has_portrait_cache:
        ref_stats_path = os.path.join(session_dir, "ref_stats.npy")
        if os.path.exists(ref_stats_path):
            ref_stats = await asyncio.to_thread(np.load, ref_stats_path)

    if not has_merged and not has_portrait_cache and not has_lut_cache and not (session_id and os.path.exists(lut_path)):
        raise HTTPException(status_code=404, detail="预览数据已过期，请重新预览后再下载")

    if has_merged and not os.path.exists(merged_lut_path):
        raise HTTPException(status_code=404, detail="合并 LUT 数据已过期，请重新追色")

    resolved_target_path = _resolve_local_file_path(target_path)
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标文件不存在")
    target_path = str(resolved_target_path)

    await prog("load", 5, "正在加载全尺寸原图...")
    await asyncio.sleep(0.01)

    target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=None, mode="export")
    if target_img is None:
        raise HTTPException(status_code=400, detail="无法读取图片")

    h, w = target_img.shape[:2]
    await prog("load", 15, f"全尺寸图片已加载 {w}x{h}")
    await asyncio.sleep(0.01)

    if size_mode == "2x":
        target_img = await asyncio.to_thread(cv2.resize, target_img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        h, w = target_img.shape[:2]
        await prog("load", 17, f"2x 超分 {w}x{h}")
    elif size_mode == "half":
        target_img = await asyncio.to_thread(cv2.resize, target_img, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        h, w = target_img.shape[:2]
        await prog("load", 17, f"50% 缩放 {w}x{h}")

    target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)

    if has_merged:
        await prog("lut_load", 20, "加载合并 LUT...")
        await asyncio.sleep(0.01)

        lut_merged = await asyncio.to_thread(np.load, merged_lut_path)

        await prog("render", 30, f"合并 LUT 渲染中 ({w}x{h})...")
        await asyncio.sleep(0.01)

        from core.render.full_render import apply_lut
        result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_merged)
        del lut_merged
    elif has_portrait_cache:
        await prog("lut_load", 20, "加载全局 LUT + 皮肤 Mask...")
        await asyncio.sleep(0.01)

        lut_global = await asyncio.to_thread(np.load, os.path.join(session_dir, "lut_global.npy"))

        def _load_mask(path):
            arr = np.fromfile(path, dtype=np.uint8)
            m = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
            return m

        mask_uint8 = await asyncio.to_thread(_load_mask, os.path.join(session_dir, "mask_soft.png"))
        lip_mask_uint8 = await asyncio.to_thread(_load_mask, os.path.join(session_dir, "lip_mask.png"))
        hair_mask_uint8 = await asyncio.to_thread(_load_mask, os.path.join(session_dir, "hair_mask.png"))
        if mask_uint8 is None:
            await prog("render", 30, f"参数化渲染中 ({w}x{h})...")
            await asyncio.sleep(0.01)
            from core.render.full_render import apply_lut
            result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_global)
            del lut_global
        else:
            mask_low_res = mask_uint8.astype(np.float32) / 255.0
            lip_mask_low_res = lip_mask_uint8.astype(np.float32) / 255.0 if lip_mask_uint8 is not None else None
            hair_mask_low_res = hair_mask_uint8.astype(np.float32) / 255.0 if hair_mask_uint8 is not None else None
            await prog("render", 30, f"人像参数化渲染中 ({w}x{h})...")
            await asyncio.sleep(0.01)
            from core.render.full_render import apply_portrait_with_lab
            result_rgb = await asyncio.to_thread(apply_portrait_with_lab, target_rgb, lut_global, mask_low_res, ref_stats, lip_mask_low_res, hair_mask_low_res)
            del lut_global, mask_uint8, mask_low_res
    elif has_lut_cache:
        await prog("lut_load", 20, "加载导入 LUT...")
        await asyncio.sleep(0.01)

        lut_global = await asyncio.to_thread(np.load, os.path.join(session_dir, "lut_global.npy"))

        await prog("render", 30, f"LUT 极速渲染中 ({w}x{h})...")
        await asyncio.sleep(0.01)

        from core.render.full_render import apply_lut
        result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_global)
        del lut_global
    else:
        await prog("lut_load", 20, "加载 3D LUT...")
        await asyncio.sleep(0.01)
        lut_3d = await asyncio.to_thread(np.load, lut_path)

        await prog("render", 30, f"参数化渲染中 ({w}x{h})...")
        await asyncio.sleep(0.01)

        from core.render.full_render import apply_lut
        result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_3d)

        del lut_3d

    result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
    result_img = await asyncio.to_thread(_apply_cached_depth_layers_if_any, target_img, result_img, session_dir)
    result_img = await asyncio.to_thread(_apply_cached_subject_mask_if_any, target_img, result_img, session_dir)

    alpha = intensity / 100.0
    e = (exposure - 100.0) * 0.02
    c = (contrast - 100.0) * 0.005
    h = (highlight - 100.0) * 0.02
    s = (shadow - 100.0) * 0.02
    v = (vibrance - 100.0) * 0.005

    if not (alpha == 1.0 and e == 0.0 and c == 0.0 and h == 0.0 and s == 0.0 and v == 0.0):
        result_img = await asyncio.to_thread(
            apply_pro_adjust, target_img, result_img, alpha, e, c, h, s, v
        )

    await prog("encode", 80, "编码输出...")
    await asyncio.sleep(0.01)

    is_uint16 = result_img.dtype == np.uint16

    try:
        if is_uint16:
            success, buf = await asyncio.to_thread(cv2.imencode, ".png", result_img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            media_type = "image/png"
            output_filename = f"{uuid.uuid4().hex}_full.png"
        elif format == "png":
            success, buf = await asyncio.to_thread(cv2.imencode, ".png", result_img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            media_type = "image/png"
            output_filename = f"{uuid.uuid4().hex}_full.png"
        else:
            success, buf = await asyncio.to_thread(cv2.imencode, ".jpg", result_img, [cv2.IMWRITE_JPEG_QUALITY, 98])
            media_type = "image/jpeg"
            output_filename = f"{uuid.uuid4().hex}_full.jpg"

        if not success or buf is None:
            raise RuntimeError("图像编码失败")

        output_bytes = buf.tobytes()
        export_size_bytes = len(output_bytes)
    except Exception as e:
        print(f"[Download] imencode failed: {e}, falling back to file write")
        output_filename = f"{uuid.uuid4().hex}_full.png"
        output_path = os.path.join(str(_runtime_upload_dir()), output_filename)
        ok, buf = await asyncio.to_thread(cv2.imencode, '.png', result_img)
        if ok:
            buf.tofile(output_path)
            write_ok = True
            try:
                export_size_bytes = os.path.getsize(output_path)
            except OSError:
                export_size_bytes = 0
        else:
            write_ok = False
        if not write_ok:
            raise HTTPException(status_code=500, detail="图像保存失败")
        del target_img, result_img, result_rgb, target_rgb
        gc.collect()
        await prog("done", 100, "全尺寸渲染完成！")
        mark_download_export()
        return FileResponse(path=output_path, filename=output_filename, media_type="image/png")

    del target_img, result_img, result_rgb, target_rgb
    gc.collect()

    await prog("done", 100, "全尺寸渲染完成！")
    mark_download_export()
    return Response(
        content=output_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{output_filename}"'},
    )


@app.post("/api/render_single")
async def api_render_single(
    target_path: str = Form(...),
    session_id: str = Form(None),
    merged_session_id: str = Form(None),
    format: str = Form("png"),
    size_mode: str = Form("full"),
    custom_long_edge: int = Form(None),
    export_both: str = Form(None),
    intensity: float = Form(100.0),
    exposure: float = Form(100.0),
    contrast: float = Form(100.0),
    highlight: float = Form(100.0),
    shadow: float = Form(100.0),
    vibrance: float = Form(100.0),
    project_id: int = Form(0),
    asset_name: str = Form(""),
    rating: int = Form(0),
    algorithm: str = Form(""),
    reference_path: str = Form(""),
    reference_data_url: str = Form(""),
    authorization: Optional[str] = Header(None),
):
    import gc

    request_user_id = _get_request_user_id(authorization)
    project_id = await _ensure_project_access(project_id, request_user_id)
    resolved_target_path = _resolve_local_file_path(target_path)
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标文件不存在")
    target_path = str(resolved_target_path)

    has_merged = bool(merged_session_id)
    merged_lut_path = os.path.join(str(_runtime_temp_lut_dir()), f"{merged_session_id}.npy") if has_merged else None
    session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id) if session_id else None
    lut_path = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}.npy") if session_id else None
    has_portrait_cache = (
        session_id and os.path.isdir(session_dir)
        and os.path.exists(os.path.join(session_dir, "lut_global.npy"))
        and os.path.exists(os.path.join(session_dir, "mask_soft.png"))
    )
    has_lut_cache = (
        session_id and os.path.isdir(session_dir)
        and os.path.exists(os.path.join(session_dir, "lut_global.npy"))
    )

    if not has_merged and not has_portrait_cache and not has_lut_cache and not (session_id and os.path.exists(lut_path)):
        raise HTTPException(status_code=404, detail="LUT 数据已过期")

    if has_merged and not os.path.exists(merged_lut_path):
        raise HTTPException(status_code=404, detail="合并 LUT 数据已过期")

    target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=None, mode="export")
    if target_img is None:
        raise HTTPException(status_code=400, detail="无法读取图片")

    h, w = target_img.shape[:2]

    if size_mode == "2x":
        target_img = await asyncio.to_thread(cv2.resize, target_img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        h, w = target_img.shape[:2]
    elif size_mode == "half":
        target_img = await asyncio.to_thread(cv2.resize, target_img, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        h, w = target_img.shape[:2]
    elif size_mode == "custom" and custom_long_edge and custom_long_edge > 0:
        long_edge = max(w, h)
        if long_edge > custom_long_edge:
            scale = custom_long_edge / long_edge
            new_w = int(w * scale)
            new_h = int(h * scale)
            target_img = await asyncio.to_thread(cv2.resize, target_img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
            h, w = target_img.shape[:2]

    target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)

    if has_merged:
        lut_merged = await asyncio.to_thread(np.load, merged_lut_path)
        from core.render.full_render import apply_lut
        result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_merged)
        del lut_merged
    elif has_portrait_cache:
        lut_global = await asyncio.to_thread(np.load, os.path.join(session_dir, "lut_global.npy"))
        def _load_mask(path):
            arr = np.fromfile(path, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        mask_uint8 = await asyncio.to_thread(_load_mask, os.path.join(session_dir, "mask_soft.png"))
        lip_mask_uint8 = await asyncio.to_thread(_load_mask, os.path.join(session_dir, "lip_mask.png"))
        hair_mask_uint8 = await asyncio.to_thread(_load_mask, os.path.join(session_dir, "hair_mask.png"))
        mask_low_res = mask_uint8.astype(np.float32) / 255.0 if mask_uint8 is not None else None
        lip_mask_low_res = lip_mask_uint8.astype(np.float32) / 255.0 if lip_mask_uint8 is not None else None
        hair_mask_low_res = hair_mask_uint8.astype(np.float32) / 255.0 if hair_mask_uint8 is not None else None
        from core.render.full_render import apply_portrait_with_lab
        result_rgb = await asyncio.to_thread(apply_portrait_with_lab, target_rgb, lut_global, mask_low_res, None, lip_mask_low_res, hair_mask_low_res)
        del lut_global
    elif has_lut_cache:
        lut_global = await asyncio.to_thread(np.load, os.path.join(session_dir, "lut_global.npy"))
        from core.render.full_render import apply_lut
        result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_global)
        del lut_global
    else:
        lut_3d = await asyncio.to_thread(np.load, lut_path)
        from core.render.full_render import apply_lut
        result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_3d)
        del lut_3d

    result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
    result_img = await asyncio.to_thread(_apply_cached_depth_layers_if_any, target_img, result_img, session_dir)
    result_img = await asyncio.to_thread(_apply_cached_subject_mask_if_any, target_img, result_img, session_dir)

    alpha = intensity / 100.0
    e = (exposure - 100.0) * 0.02
    c = (contrast - 100.0) * 0.005
    h_adj = (highlight - 100.0) * 0.02
    s = (shadow - 100.0) * 0.02
    v = (vibrance - 100.0) * 0.005

    if not (alpha == 1.0 and e == 0.0 and c == 0.0 and h_adj == 0.0 and s == 0.0 and v == 0.0):
        result_img = await asyncio.to_thread(
            apply_pro_adjust, target_img, result_img, alpha, e, c, h_adj, s, v
        )

    is_uint16 = result_img.dtype == np.uint16
    if is_uint16 or format == "png":
        success, buf = await asyncio.to_thread(cv2.imencode, ".png", result_img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        media_type = "image/png"
        result_ext = ".png"
    else:
        success, buf = await asyncio.to_thread(cv2.imencode, ".jpg", result_img, [cv2.IMWRITE_JPEG_QUALITY, 98])
        media_type = "image/jpeg"
        result_ext = ".jpg"

    if not success or buf is None:
        raise HTTPException(status_code=500, detail="图像编码失败")

    output_bytes = buf.tobytes()
    project_export_url = ""
    if project_id > 0:
        try:
            result_name_source = Path(str(asset_name or Path(target_path).name)).stem or "export"
            result_name_source = re.sub(r"[^A-Za-z0-9._-]+", "_", result_name_source).strip("._-") or "export"
            export_name = f"{result_name_source}_{uuid.uuid4().hex[:10]}{result_ext}"
            export_path, project_export_url = _project_bucket_file(project_id, "exports", export_name)
            export_path.write_bytes(output_bytes)
        except Exception as exc:
            print(f"[Project Assets] image export save failed: {exc}")
    if rating > 0:
        try:
            await _archive_training_sample(
                user_id=request_user_id,
                project_id=project_id,
                asset_name=asset_name,
                target_path=target_path,
                reference_path=reference_path,
                reference_data_url=reference_data_url,
                result_bytes=output_bytes,
                result_ext=result_ext,
                rating=rating,
                algorithm=algorithm,
                session_id=session_id or "",
                merged_session_id=merged_session_id or "",
                export_format=format,
                size_mode=size_mode,
                params={
                    "intensity": intensity,
                    "exposure": exposure,
                    "contrast": contrast,
                    "highlight": highlight,
                    "shadow": shadow,
                    "vibrance": vibrance,
                    "custom_long_edge": custom_long_edge,
                    "export_both": bool(export_both),
                },
            )
        except Exception as exc:
            print(f"[Training Corpus] archive failed: {exc}")

    del target_img, result_img, result_rgb, target_rgb
    gc.collect()

    headers = {}
    if project_export_url:
        headers["X-ColorChase-Project-Asset-Url"] = project_export_url
    return Response(content=output_bytes, media_type=media_type, headers=headers)


@app.post("/api/video_metadata")
async def api_video_metadata(video: UploadFile = File(...)):
    ensure_upload_file_size(video, 300 * 1024 * 1024, label="视频文件")
    tmp_path = None
    cap = None
    try:
        tmp_dir = tempfile.gettempdir()
        tmp_name = uuid.uuid4().hex + os.path.splitext(video.filename or "video.mp4")[1]
        tmp_path = os.path.join(tmp_dir, tmp_name)
        contents = await video.read()
        with open(tmp_path, "wb") as f:
            f.write(contents)
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            raise ValueError("无法打开视频文件")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 25.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = frame_count / fps if fps > 0 else 0
        codec = "h264"
        try:
            fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
            codec = "".join([chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)]).strip()
        except Exception:
            pass
        return JSONResponse({
            "fps": round(fps, 2),
            "duration": round(duration, 2),
            "width": width,
            "height": height,
            "codec": codec,
            "frame_count": frame_count,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if cap is not None:
            cap.release()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8033)
