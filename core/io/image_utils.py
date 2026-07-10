import base64
import os
import uuid
from typing import Optional

import cv2
import numpy as np
from fastapi import HTTPException, UploadFile

from app.services.paths import (
    _normalize_project_id,
    _runtime_upload_dir,
    _save_to_runtime_user_temp,
    _safe_project_bucket_dir,
)
from core.io.loaders import load_image_bgr

PREVIEW_MAX_SIZE = 1024
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".mp4", ".mov", ".avi"}


def _save_upload(
    file: UploadFile,
    project_id: int = 0,
    bucket: str = "uploads",
    user_id: int = None,
    is_admin: bool = True,
    storage_label: Optional[str] = None,
) -> str:
    """保存上传文件。管理员写入 project bucket 或上传目录；普通用户写入临时目录，不落盘。"""
    ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    if ext.lower() not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    filename = f"{uuid.uuid4().hex}{ext}"
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"上传文件为空: {file.filename or '未命名文件'}")
    if is_admin and _normalize_project_id(project_id) > 0:
        filepath = _safe_project_bucket_dir(project_id, bucket, storage_label=storage_label) / filename
        with open(filepath, "wb") as f:
            f.write(content)
    elif is_admin:
        filepath = os.path.join(str(_runtime_upload_dir()), filename)
        with open(filepath, "wb") as f:
            f.write(content)
    else:
        filepath = _save_to_runtime_user_temp(content, user_id, filename, storage_label=storage_label)
    return str(filepath)


def _cv2_imread_full(filepath) -> Optional[np.ndarray]:
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
