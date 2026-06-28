import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

from .segmentation import detect_person_region


DEPTH_ANYTHING_WEIGHT_CANDIDATES = (
    "weights/depth_anything_v2/depth_anything_v2_vits.pth",
    "weights/depth_anything_v2/depth_anything_v2_vitb.pth",
    "weights/depth_anything_v2/depth_anything_v2_vitl.pth",
    "models/depth_anything_v2/depth_anything_v2_vits.pth",
    "models/depth_anything_v2/depth_anything_v2_vitb.pth",
    "models/depth_anything_v2/depth_anything_v2_vitl.pth",
)

_depth_model_cache = {}


def get_depth_anything_status(base_dir: Path) -> Dict:
    files = []
    for rel_path in DEPTH_ANYTHING_WEIGHT_CANDIDATES:
        path = base_dir / rel_path
        files.append({
            "path": str(path),
            "exists": path.exists(),
            "size_mb": round(path.stat().st_size / (1024 * 1024), 1) if path.exists() else 0.0,
        })

    has_weight = any(item["exists"] for item in files)
    for index, item in enumerate(files):
        item["required"] = (not has_weight and index == 0)
    runtime_ready = importlib.util.find_spec("depth_anything_v2") is not None
    torch_ready = importlib.util.find_spec("torch") is not None
    model_ready = has_weight and runtime_ready and torch_ready

    return {
        "ready": True,
        "model_ready": model_ready,
        "runtime_ready": runtime_ready,
        "torch_ready": torch_ready,
        "fallback_ready": True,
        "files": files,
        "missing_files": [item["path"] for item in files if item.get("required", True) and not item["exists"]],
        "note": (
            "Depth Anything V2 runtime and weights are ready."
            if model_ready else
            "Depth Anything V2 runtime or weights are missing; using local heuristic depth fallback."
        ),
    }


def build_depth_cache_key(image_path: str, model_choice: str = "auto", version: str = "depth-layer-v1") -> str:
    try:
        st = os.stat(image_path)
        stat_sig = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stat_sig = "missing"
    payload = {
        "version": version,
        "path": str(Path(image_path).resolve()),
        "stat": stat_sig,
        "model": model_choice or "auto",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def load_depth_file(depth_path: str, shape: Tuple[int, int]) -> Optional[np.ndarray]:
    if not depth_path or not os.path.exists(depth_path):
        return None
    arr = np.fromfile(depth_path, dtype=np.uint8)
    if arr.size == 0:
        return None
    depth_u8 = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if depth_u8 is None:
        return None
    h, w = shape[:2]
    if depth_u8.shape[:2] != (h, w):
        depth_u8 = cv2.resize(depth_u8, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(depth_u8.astype(np.float32) / 255.0, 0.0, 1.0)


def save_depth_png(depth: np.ndarray, output_path: str) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    depth_u8 = np.clip(depth * 255.0, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".png", depth_u8)
    if not ok or buf is None:
        raise RuntimeError("depth encode failed")
    buf.tofile(str(out))


def generate_depth_map(
    img_bgr: np.ndarray,
    base_dir: Optional[Path] = None,
    model_choice: str = "auto",
) -> Tuple[np.ndarray, Dict]:
    model_choice = str(model_choice or "auto").lower()
    if base_dir is not None and model_choice not in ("fallback", "heuristic", "fast"):
        try:
            depth = _infer_depth_anything_v2(img_bgr, base_dir)
            if depth is not None:
                depth = _normalize_depth(depth)
                meta = _depth_meta("depth_anything_v2", depth)
                meta["model_choice"] = model_choice
                return _smooth_depth(img_bgr, depth), meta
        except Exception as exc:
            print(f"[DepthAnythingV2] fallback: {exc}")

    depth = _heuristic_depth(img_bgr)
    meta = _depth_meta("heuristic_depth", depth)
    meta["model_choice"] = model_choice
    return depth, meta


def apply_depth_layer_blend(
    target_img: np.ndarray,
    result_img: np.ndarray,
    depth: np.ndarray,
    strength: float = 0.65,
    foreground_protection: float = 0.55,
    background_boost: float = 0.25,
) -> np.ndarray:
    if depth is None:
        return result_img
    h, w = target_img.shape[:2]
    if depth.shape[:2] != (h, w):
        depth = cv2.resize(depth, (w, h), interpolation=cv2.INTER_LINEAR)

    strength = float(np.clip(strength, 0.0, 1.0))
    foreground_protection = float(np.clip(foreground_protection, 0.0, 0.85))
    background_boost = float(np.clip(background_boost, 0.0, 0.65))
    depth = _smooth_depth(target_img, np.clip(depth.astype(np.float32), 0.0, 1.0))

    fg = _sigmoid((depth - 0.62) / 0.08)
    bg = _sigmoid((0.38 - depth) / 0.08)
    layer_amount = 1.0 - foreground_protection * fg + background_boost * bg
    layer_amount = np.clip(layer_amount, 0.0, 1.0)
    transfer_amount = 1.0 - strength * (1.0 - layer_amount)
    transfer_amount = cv2.GaussianBlur(transfer_amount.astype(np.float32), (0, 0), sigmaX=max(2, min(h, w) / 220.0))
    transfer_amount = np.clip(transfer_amount, 0.0, 1.0)[:, :, np.newaxis]

    mixed = target_img.astype(np.float32) * (1.0 - transfer_amount) + result_img.astype(np.float32) * transfer_amount
    return np.clip(mixed, 0, 255).astype(result_img.dtype)


def _infer_depth_anything_v2(img_bgr: np.ndarray, base_dir: Path) -> Optional[np.ndarray]:
    weight_path = _find_depth_weight(base_dir)
    if weight_path is None or importlib.util.find_spec("depth_anything_v2") is None:
        return None

    import torch
    from depth_anything_v2.dpt import DepthAnythingV2

    encoder = _encoder_from_weight(weight_path.name)
    cache_key = str(weight_path)
    model = _depth_model_cache.get(cache_key)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if model is None:
        configs = {
            "vits": {"encoder": "vits", "features": 64, "out_channels": [48, 96, 192, 384]},
            "vitb": {"encoder": "vitb", "features": 128, "out_channels": [96, 192, 384, 768]},
            "vitl": {"encoder": "vitl", "features": 256, "out_channels": [256, 512, 1024, 1024]},
        }
        model = DepthAnythingV2(**configs[encoder])
        state = torch.load(str(weight_path), map_location="cpu")
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        model.load_state_dict(state, strict=False)
        model = model.to(device).eval()
        _depth_model_cache[cache_key] = model

    with torch.no_grad():
        depth = model.infer_image(img_bgr, input_size=518)
    return np.asarray(depth, dtype=np.float32)


def _find_depth_weight(base_dir: Path) -> Optional[Path]:
    for rel_path in DEPTH_ANYTHING_WEIGHT_CANDIDATES:
        path = base_dir / rel_path
        if path.exists():
            return path
    return None


def _encoder_from_weight(name: str) -> str:
    lowered = name.lower()
    if "vitl" in lowered:
        return "vitl"
    if "vitb" in lowered:
        return "vitb"
    return "vits"


def _heuristic_depth(img_bgr: np.ndarray) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    y = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    vertical_near = np.repeat(y, w, axis=1)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w * 0.5, h * 0.54
    dist = ((xx - cx) / max(w * 0.55, 1)) ** 2 + ((yy - cy) / max(h * 0.55, 1)) ** 2
    center_near = np.clip(1.0 - dist, 0.0, 1.0)

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Laplacian(gray, cv2.CV_32F)
    edge_near = np.abs(edges)
    edge_near = cv2.GaussianBlur(edge_near, (0, 0), 2.0)
    edge_near = _normalize_depth(edge_near)

    try:
        person = detect_person_region(img_bgr)
        person_near = np.clip(person.astype(np.float32), 0.0, 1.0)
    except Exception:
        person_near = np.zeros((h, w), dtype=np.float32)

    depth = (
        vertical_near * 0.30
        + center_near * 0.18
        + edge_near * 0.12
        + person_near * 0.40
    )
    if float(person_near.max()) <= 0:
        depth = vertical_near * 0.46 + center_near * 0.34 + edge_near * 0.20
    return _smooth_depth(img_bgr, _normalize_depth(depth))


def _smooth_depth(img_bgr: np.ndarray, depth: np.ndarray) -> np.ndarray:
    depth = np.clip(depth.astype(np.float32), 0.0, 1.0)
    h, w = depth.shape[:2]
    depth_u8 = (depth * 255).astype(np.uint8)
    try:
        guide = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        refined = cv2.ximgproc.guidedFilter(guide, depth_u8, radius=12, eps=1e-2)
        depth = refined.astype(np.float32) / 255.0
    except Exception:
        depth = cv2.bilateralFilter(depth_u8, 9, 60, 60).astype(np.float32) / 255.0
    depth = cv2.GaussianBlur(depth, (0, 0), sigmaX=max(1.5, min(h, w) / 360.0))
    return np.clip(depth, 0.0, 1.0).astype(np.float32)


def _normalize_depth(depth: np.ndarray) -> np.ndarray:
    depth = depth.astype(np.float32)
    finite = np.isfinite(depth)
    if not finite.any():
        return np.zeros(depth.shape[:2], dtype=np.float32)
    lo, hi = np.percentile(depth[finite], [1, 99])
    if hi - lo < 1e-6:
        return np.zeros(depth.shape[:2], dtype=np.float32)
    return np.clip((depth - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))


def _depth_meta(source: str, depth: np.ndarray) -> Dict:
    return {
        "source": source,
        "near_mean": round(float(np.mean(depth)), 4),
        "near_p15": round(float(np.percentile(depth, 15)), 4),
        "near_p85": round(float(np.percentile(depth, 85)), 4),
    }
