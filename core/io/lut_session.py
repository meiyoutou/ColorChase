import json
import os
import uuid
from datetime import datetime

import cv2
import numpy as np

from app.services.paths import _runtime_temp_lut_dir
from app.settings import USER_SPACE_TZ
from config import STORAGE_STYLES_EXTRACTED_DIR

STYLES_EXTRACTED_DIR = STORAGE_STYLES_EXTRACTED_DIR


def _load_lut_for_session(session_id: str) -> np.ndarray:
    if os.path.exists(session_id) and session_id.endswith('.npy'):
        return np.load(session_id)
    session_dir = os.path.join(str(_runtime_temp_lut_dir()), session_id)
    lut_global = os.path.join(session_dir, "lut_global.npy")
    if os.path.exists(lut_global):
        return np.load(lut_global)
    lut_direct = os.path.join(str(_runtime_temp_lut_dir()), f"{session_id}.npy")
    if os.path.exists(lut_direct):
        return np.load(lut_direct)
    raise FileNotFoundError(f"LUT not found for session {session_id}")


def _save_lut_as_style_preset(lut_3d: np.ndarray, thumbnail_img, source_label: str = "DNCM LUT") -> dict:
    style_id = "dncm_" + datetime.now(USER_SPACE_TZ).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    style_dir = STYLES_EXTRACTED_DIR / style_id
    style_dir.mkdir(parents=True, exist_ok=True)

    lut_path = style_dir / "lut_global.npy"
    np.save(lut_path, lut_3d.astype(np.float32))

    thumb_path = style_dir / "thumbnail.jpg"
    try:
        preview = thumbnail_img
        if preview is not None:
            h, w = preview.shape[:2]
            scale = min(1.0, 360.0 / max(h, w))
            if scale < 1.0:
                preview = cv2.resize(preview, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tofile(str(thumb_path))
    except Exception as exc:
        print(f"[Style Preset] thumbnail save failed: {exc}")

    style_name = f"{source_label} {datetime.now(USER_SPACE_TZ).strftime('%m-%d %H:%M')}"
    ccs_path = style_dir / "style.ccs"
    ccs_path.write_text(json.dumps({
        "name": style_name,
        "camera": "ColorChase",
        "source": source_label,
        "created_at": datetime.now(USER_SPACE_TZ).isoformat(),
        "lut_file": "lut_global.npy",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "id": style_id,
        "name": style_name,
        "path": str(lut_path),
        "thumbnail": f"/styles/extracted/{style_id}/thumbnail.jpg" if thumb_path.exists() else "",
    }
