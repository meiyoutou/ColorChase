import asyncio
import io
import os
import struct
import uuid
import zipfile

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.services.paths import _resolve_local_file_path, _runtime_temp_lut_dir
from app.security import ensure_upload_file_size
from core.color.lut_ops import _build_identity_lut, _generate_builtin_profile, _trilinear_lookup
from core.io.image_utils import _cv2_imread, _img_to_base64
from core.io.lut_session import _load_lut_for_session
from core.render.full_render import apply_lut

router = APIRouter()


def _create_minimal_dng(width=64, height=64):
    raw_data = np.zeros((height, width), dtype=np.uint16)
    xs = np.arange(width, dtype=np.float32)
    ys = np.arange(height, dtype=np.float32)
    xx, yy = np.meshgrid(xs, ys)
    raw_data = np.clip(65535 * (xx + yy) / max(width + height - 2, 1), 0, 65535).astype(np.uint16)
    raw = raw_data.tobytes()
    strip_size = len(raw)

    cfa_dim_value = struct.unpack('<I', struct.pack('<HH', 2, 2))[0]
    cfa_pattern_value = struct.unpack('<I', struct.pack('BBBB', 0, 1, 1, 2))[0]
    dng_ver_value = struct.unpack('<I', struct.pack('BBBB', 1, 4, 0, 0))[0]
    dng_bw_value = struct.unpack('<I', struct.pack('BBBB', 1, 1, 0, 0))[0]

    camera_model = b'ColorChase Virtual Camera\x00'
    num_tags = 14
    ifd_end = 8 + 2 + num_tags * 12 + 4
    camera_offset = ifd_end
    img_offset = camera_offset + len(camera_model)

    entries = [
        (256, 3, 1, width),
        (257, 3, 1, height),
        (258, 3, 1, 16),
        (259, 3, 1, 1),
        (262, 3, 1, 32803),
        (273, 4, 1, img_offset),
        (277, 3, 1, 1),
        (278, 3, 1, height),
        (279, 4, 1, strip_size),
        (33421, 3, 2, cfa_dim_value),
        (33422, 1, 4, cfa_pattern_value),
        (50706, 1, 4, dng_ver_value),
        (50707, 1, 4, dng_bw_value),
        (50708, 2, len(camera_model), camera_offset),
    ]

    entries.sort(key=lambda e: e[0])

    buf = b''
    buf += struct.pack('<2sHI', b'II', 42, 8)
    buf += struct.pack('<H', num_tags)
    for tag, typ, count, value in entries:
        buf += struct.pack('<HHII', tag, typ, count, value)
    buf += struct.pack('<I', 0)
    buf += camera_model
    buf += raw

    return buf


@router.post("/api/prepare_lr_preset")
async def api_prepare_lr_preset(
    xmp_file: UploadFile = File(...),
    style_name: str = Form("Untitled"),
):
    if not xmp_file or not xmp_file.filename:
        raise HTTPException(status_code=400, detail="请提供 XMP 文件")

    ensure_upload_file_size(xmp_file, 10 * 1024 * 1024, label="XMP 文件")
    xmp_bytes = await xmp_file.read()

    dng_bytes = await asyncio.to_thread(_create_minimal_dng, 64, 64)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        dng_name = f"{style_name}.dng"
        zf.writestr(dng_name, dng_bytes)
        xmp_sidecar = f"{style_name}.xmp"
        zf.writestr(xmp_sidecar, xmp_bytes)
        zf.writestr("高精度模式说明.txt", f"Lightroom 预设：{style_name}\n\n1. 在 Lightroom 中打开 DNG 文件\n2. XMP 预设将自动加载应用\n3. 导出为 JPG 格式\n4. 将导出的 JPG 拖回本页面上传，即可获得 98% 高保真还原".encode("utf-8"))

    zip_buffer.seek(0)
    zip_filename = f"{style_name}_dng_pack.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@router.post("/api/merge_luts")
async def api_merge_luts(
    ai_session_id: str = Form(None),
    profile_session_id: str = Form(None),
    profile_builtin: str = Form(None),
    target_path: str = Form(None),
):
    lut_ai = None
    lut_profile = None

    if ai_session_id:
        try:
            lut_ai = await asyncio.to_thread(_load_lut_for_session, ai_session_id)
        except FileNotFoundError:
            pass

    if profile_session_id:
        try:
            lut_profile = await asyncio.to_thread(_load_lut_for_session, profile_session_id)
        except FileNotFoundError:
            pass
    elif profile_builtin:
        lut_profile = await asyncio.to_thread(_generate_builtin_profile, profile_builtin)

    if lut_profile is None:
        lut_profile = _build_identity_lut(33)

    if lut_ai is None:
        lut_ai = _build_identity_lut(33)

    size = lut_ai.shape[0]
    grid_flat = np.stack(np.meshgrid(
        np.linspace(0, 1, size),
        np.linspace(0, 1, size),
        np.linspace(0, 1, size),
        indexing="ij"
    ), axis=-1).reshape(-1, 3)

    ai_mapped = _trilinear_lookup(lut_ai, grid_flat)
    merged_flat = _trilinear_lookup(lut_profile, ai_mapped)
    lut_merged = merged_flat.reshape(size, size, size, 3)

    merged_id = uuid.uuid4().hex
    merged_path = os.path.join(str(_runtime_temp_lut_dir()), f"{merged_id}.npy")
    await asyncio.to_thread(np.save, merged_path, lut_merged)

    result_b64 = None
    resolved_target_path = _resolve_local_file_path(target_path)
    if resolved_target_path:
        target_path = str(resolved_target_path)
        target_img = await asyncio.to_thread(_cv2_imread, target_path, target_size=1024)
        if target_img is not None:
            target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            result_rgb = await asyncio.to_thread(apply_lut, target_rgb, lut_merged)
            result_img = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
            result_b64 = f"data:image/png;base64,{await asyncio.to_thread(_img_to_base64, result_img, '.png')}"

    return JSONResponse({
        "success": True,
        "merged_session_id": merged_id,
        "merged_lut_path": merged_path,
        "result_b64": result_b64,
    })
