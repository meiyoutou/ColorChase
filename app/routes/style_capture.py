import os
import uuid
import shutil
import tempfile
import asyncio
import numpy as np
import cv2
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from app.security import ensure_upload_file_size
from config import STORAGE_STYLES_EXTRACTED_DIR

router = APIRouter()

STYLES_DIR = str(STORAGE_STYLES_EXTRACTED_DIR)


def _resize_long_edge(img, long_edge=1024):
    h, w = img.shape[:2]
    if max(h, w) <= long_edge:
        return img
    scale = long_edge / max(h, w)
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _read_raw_linear(raw_path):
    import rawpy
    ext = os.path.splitext(raw_path)[1].lower()
    try:
        raw = rawpy.imread(raw_path)
    except Exception:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_raw = os.path.join(tmp_dir, "raw_temp" + ext)
            shutil.copy2(raw_path, tmp_raw)
            raw = rawpy.imread(tmp_raw)

    try:
        linear_bgr16 = raw.postprocess(
            output_bps=16,
            gamma=(1, 1),
            no_auto_bright=True,
            use_camera_wb=True,
        )
    finally:
        raw.close()

    linear_img = linear_bgr16.astype(np.float32) / 65535.0
    linear_rgb = cv2.cvtColor(linear_img, cv2.COLOR_BGR2RGB)
    return linear_rgb


def _read_jpg_float(jpg_path):
    arr = np.fromfile(jpg_path, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        bgr = cv2.imread(jpg_path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"Cannot read JPG: {jpg_path}")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return rgb.astype(np.float32) / 255.0


def _extract_camera_from_exif(filepath):
    try:
        from core.io.metadata.exif_parser import parse_exif
        exif = parse_exif(filepath)
        make = exif.get("Make", "")
        model = exif.get("Model", "")
        if make and model:
            return f"{make} {model}".strip()
        if model:
            return model.strip()
    except Exception:
        pass
    return "Unknown"


@router.post("/api/capture_style")
async def api_capture_style(
    raw_file: UploadFile = File(...),
    camera_jpg: UploadFile = File(...),
):
    ensure_upload_file_size(raw_file, 300 * 1024 * 1024, label="RAW 文件")
    ensure_upload_file_size(camera_jpg, 10 * 1024 * 1024, label="相机 JPG")
    raw_ext = os.path.splitext(raw_file.filename)[1].lower() if raw_file.filename else ""
    jpg_ext = os.path.splitext(camera_jpg.filename)[1].lower() if camera_jpg.filename else ""

    raw_bytes = await raw_file.read()
    jpg_bytes = await camera_jpg.read()

    with tempfile.TemporaryDirectory() as tmp_dir:
        raw_tmp = os.path.join(tmp_dir, "input" + raw_ext)
        jpg_tmp = os.path.join(tmp_dir, "camera" + jpg_ext)

        with open(raw_tmp, "wb") as f:
            f.write(raw_bytes)
        with open(jpg_tmp, "wb") as f:
            f.write(jpg_bytes)

        try:
            linear_rgb = await asyncio.to_thread(_read_raw_linear, raw_tmp)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"RAW 解码失败: {str(e)}")

        try:
            camera_rgb = await asyncio.to_thread(_read_jpg_float, jpg_tmp)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"JPG 读取失败: {str(e)}")

    linear_rgb = _resize_long_edge(linear_rgb, 1024)
    camera_rgb = _resize_long_edge(camera_rgb, 1024)

    h = min(linear_rgb.shape[0], camera_rgb.shape[0])
    w = min(linear_rgb.shape[1], camera_rgb.shape[1])
    linear_rgb = cv2.resize(linear_rgb, (w, h), interpolation=cv2.INTER_AREA)
    camera_rgb = cv2.resize(camera_rgb, (w, h), interpolation=cv2.INTER_AREA)

    linear_rgb = np.clip(linear_rgb, 0.0, 1.0).astype(np.float32)
    camera_rgb = np.clip(camera_rgb, 0.0, 1.0).astype(np.float32)

    try:
        from core.color.lut_extractor import extract_lut_from_pair
        lut_3d = await asyncio.to_thread(extract_lut_from_pair, linear_rgb, camera_rgb, 33)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LUT 提取失败: {str(e)}")

    style_name = os.path.splitext(raw_file.filename)[0] if raw_file.filename else f"style_{uuid.uuid4().hex[:8]}"
    style_name = style_name.replace(" ", "_")

    camera_info = "Unknown"
    with tempfile.TemporaryDirectory() as tmp_dir2:
        raw_tmp2 = os.path.join(tmp_dir2, "exif_raw" + raw_ext)
        with open(raw_tmp2, "wb") as f:
            f.write(raw_bytes)
        camera_info = _extract_camera_from_exif(raw_tmp2)

    from core.style.style_schema import create_style_dict, save_style_dict, save_lut_as_npy, save_lut_as_cube

    style_dir = os.path.join(STYLES_DIR, style_name)
    os.makedirs(style_dir, exist_ok=True)

    npy_path = await asyncio.to_thread(save_lut_as_npy, lut_3d, style_dir)
    cube_path = await asyncio.to_thread(save_lut_as_cube, lut_3d, style_dir)

    style = create_style_dict()
    style["name"] = style_name
    style["camera"] = camera_info
    style["lut3d_path"] = npy_path

    ccs_path = await asyncio.to_thread(save_style_dict, style, style_dir)

    try:
        camera_bgr = cv2.cvtColor((camera_rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        h, w = camera_bgr.shape[:2]
        sz = min(h, w)
        y0 = (h - sz) // 2
        x0 = (w - sz) // 2
        crop = camera_bgr[y0:y0+sz, x0:x0+sz]
        thumb = cv2.resize(crop, (120, 120), interpolation=cv2.INTER_AREA)
        thumb_path = os.path.join(style_dir, "thumbnail.jpg")
        cv2.imencode('.jpg', thumb, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(thumb_path)
    except Exception:
        pass

    return JSONResponse({
        "success": True,
        "style": style,
        "npy_path": npy_path,
        "cube_path": cube_path,
        "ccs_path": ccs_path,
    })
