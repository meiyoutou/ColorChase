import asyncio
import json
import os
import time
import uuid
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, Form, Header, HTTPException
from fastapi.responses import JSONResponse

from admin_runtime_metrics import record_model_call, record_task_log, record_user_usage
from app.routes.projects import _user_profile_record
from app.services.auth_utils import _get_request_user_id, _get_request_user_role
from app.services.paths import (
    _is_admin_request,
    _iter_style_extracted_roots,
    _resolve_local_file_path,
    _resolve_style_dir,
    _runtime_temp_lut_dir,
    _safe_session_dir,
)
from app.services.user_identity import resolve_user_storage_label
from app.services.task_logging import create_task_log_writer
from config import BASE_DIR
from core.io.image_utils import _cv2_imread, _img_to_base64
from core.render.full_render import apply_lut

router = APIRouter()

_write_task_log = create_task_log_writer(BASE_DIR, _user_profile_record, record_task_log)


@router.get("/api/list_styles")
async def api_list_styles():
    styles = []
    seen = set()
    style_dirs = []
    for root in _iter_style_extracted_roots():
        if not root.exists():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name in seen:
                continue
            seen.add(folder.name)
            style_dirs.append(folder)
    for folder in style_dirs:
        folder_name = folder.name
        folder_path = str(folder)
        ccs_path = os.path.join(folder_path, "style.ccs")
        name = folder_name
        camera = ""
        if os.path.exists(ccs_path):
            try:
                with open(ccs_path, "r", encoding="utf-8") as f:
                    ccs = json.load(f)
                name = ccs.get("name", folder_name)
                camera = ccs.get("camera", "")
            except Exception:
                pass
        thumb_path = f"/styles/extracted/{folder_name}/thumbnail.jpg"
        thumb_exists = os.path.exists(os.path.join(folder_path, "thumbnail.jpg"))
        styles.append({
            "id": folder_name,
            "name": name,
            "camera": camera,
            "thumbnail": thumb_path if thumb_exists else "",
        })
    return JSONResponse(styles)


@router.get("/api/get_style/{style_id}")
async def api_get_style(style_id: str):
    style_dir = _resolve_style_dir(style_id)
    if not style_dir:
        raise HTTPException(status_code=404, detail="风格未找到")
    npy_path = os.path.join(str(style_dir), "lut_global.npy")
    if not os.path.exists(npy_path):
        raise HTTPException(status_code=404, detail="LUT文件未找到")
    return JSONResponse({"npy_path": npy_path})


@router.post("/api/rename_style")
async def api_rename_style(
    style_id: str = Form(...),
    new_name: str = Form(...),
    authorization: Optional[str] = Header(None),
):
    if not _is_admin_request(authorization):
        raise HTTPException(status_code=403, detail="风格重命名仅限管理员")
    resolved_style_dir = _resolve_style_dir(style_id)
    if not resolved_style_dir:
        raise HTTPException(status_code=404, detail="风格不存在")
    style_dir = str(resolved_style_dir)
    ccs_path = os.path.join(style_dir, "style.ccs")
    if not os.path.exists(ccs_path):
        raise HTTPException(status_code=404, detail="风格配置不存在")
    try:
        with open(ccs_path, "r", encoding="utf-8") as f:
            ccs = json.load(f)
    except Exception:
        raise HTTPException(status_code=500, detail="风格配置读取失败")
    ccs["name"] = new_name
    try:
        with open(ccs_path, "w", encoding="utf-8") as f:
            json.dump(ccs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")
    return JSONResponse({"success": True, "name": new_name})


@router.post("/api/apply_style")
async def api_apply_style(
    target_path: str = Form(...),
    style_id: str = Form(...),
    session_id: str = Form(None),
    authorization: Optional[str] = Header(None),
):
    request_user_id = _get_request_user_id(authorization)
    if request_user_id is None:
        raise HTTPException(status_code=401, detail="请先登录")
    request_user_role = _get_request_user_role(authorization)
    request_started_at = time.time()
    if request_user_id is not None:
        record_user_usage(request_user_id)
    request_storage_label = await resolve_user_storage_label(request_user_id)
    record_model_call("neural_preset")
    resolved_style_dir = _resolve_style_dir(style_id)
    if not resolved_style_dir:
        raise HTTPException(status_code=404, detail="风格不存在")
    style_dir = str(resolved_style_dir)
    style_lut_path = os.path.join(style_dir, "lut_global.npy")
    if not os.path.exists(style_lut_path):
        raise HTTPException(status_code=404, detail="风格 LUT 不存在")

    style_lut = await asyncio.to_thread(np.load, style_lut_path)
    resolved_target_path = _resolve_local_file_path(
        target_path,
        request_user_id=request_user_id,
        request_storage_label=request_storage_label,
    )
    if not resolved_target_path:
        raise HTTPException(status_code=400, detail="目标图片不存在或路径无效")
    target_path = str(resolved_target_path)

    if session_id:
        _safe_session_dir(session_id)
        new_session_id = session_id
    else:
        new_session_id = uuid.uuid4().hex
        _safe_session_dir(new_session_id)
    session_dir = os.path.join(str(_runtime_temp_lut_dir()), new_session_id)

    if session_id:
        ai_lut_path = os.path.join(session_dir, "lut_global.npy")
        if os.path.exists(ai_lut_path):
            ai_lut = await asyncio.to_thread(np.load, ai_lut_path)
            size = ai_lut.shape[0]
            grid = np.linspace(0, 1, size)
            r_idx, g_idx, b_idx = np.meshgrid(grid, grid, grid, indexing='ij')
            coords_img = np.stack([r_idx, g_idx, b_idx], axis=-1).reshape(-1, 1, 3).astype(np.float32)
            sampled_img = apply_lut(coords_img, ai_lut)
            sampled = sampled_img.reshape(size, size, size, 3)
            merged_img = apply_lut(sampled.reshape(-1, 1, 3), style_lut)
            merged_lut = merged_img.reshape(size, size, size, 3)
        else:
            merged_lut = style_lut
    else:
        merged_lut = style_lut

    merged_path = os.path.join(session_dir, "lut_global.npy")
    if os.path.exists(merged_path):
        os.remove(merged_path)
    await asyncio.to_thread(np.save, merged_path, merged_lut)

    target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=1024)
    if target_img is None:
        raise HTTPException(status_code=400, detail="无法读取目标图片")

    target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
    result_rgb = await asyncio.to_thread(apply_lut, target_rgb, merged_lut)
    result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)

    result_b64 = await asyncio.to_thread(_img_to_base64, result_img, ".png")
    original_b64 = await asyncio.to_thread(_img_to_base64, target_img, ".png")
    _write_task_log(
        task_id="",
        task_type="图片追色",
        event_type="result",
        status="ok",
        summary="风格应用完成",
        detail=style_id,
        user_id=request_user_id,
        role=request_user_role,
        model="style_lut",
        duration_ms=int(max(0, (time.time() - request_started_at) * 1000)),
        meta={"source": "apply_style"},
    )

    return JSONResponse({
        "success": True,
        "merged_session_id": new_session_id,
        "result_b64": f"data:image/png;base64,{result_b64}",
        "original_b64": f"data:image/png;base64,{original_b64}",
    })
