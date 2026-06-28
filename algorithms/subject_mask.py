import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


BIREFNET_MODEL_ID = "ZhengPeng7/BiRefNet"
SAM_WEIGHT_CANDIDATES = (
    "weights/sam2/sam2_hiera_tiny.pt",
    "weights/sam2/sam2_hiera_small.pt",
    "weights/sam2/sam2_hiera_base_plus.pt",
    "weights/sam/sam_vit_b_01ec64.pth",
    "models/sam2/sam2_hiera_tiny.pt",
    "models/sam/sam_vit_b_01ec64.pth",
)
BIREFNET_CACHE = None
BIREFNET_ERROR = None


def get_subject_mask_status(base_dir: Path) -> Dict:
    files = []
    for rel_path in SAM_WEIGHT_CANDIDATES:
        path = base_dir / rel_path
        files.append({
            "path": str(path),
            "exists": path.exists(),
            "size_mb": round(path.stat().st_size / (1024 * 1024), 1) if path.exists() else 0.0,
        })

    has_sam_weight = any(item["exists"] for item in files)
    for index, item in enumerate(files):
        item["required"] = (not has_sam_weight and index == 0)
    has_sam_runtime = (
        importlib.util.find_spec("segment_anything") is not None
        or importlib.util.find_spec("sam2") is not None
    )
    sam_ready = has_sam_weight and has_sam_runtime
    mediapipe_ready = importlib.util.find_spec("mediapipe") is not None
    birefnet_status = get_birefnet_status(base_dir)

    return {
        "ready": sam_ready or mediapipe_ready or birefnet_status["model_ready"],
        "sam_ready": sam_ready,
        "sam_runtime_ready": has_sam_runtime,
        "birefnet_ready": birefnet_status["model_ready"],
        "birefnet_runtime_ready": birefnet_status["runtime_ready"],
        "fallback_ready": mediapipe_ready,
        "files": files,
        "missing_files": [item["path"] for item in files if item.get("required", True) and not item["exists"]],
        "note": (
            "SAM/SAM2 权重和运行库已发现，可升级为 SAM 主体分割入口。"
            if sam_ready else
            "SAM/SAM2 权重或运行库未完整接入，当前使用 MediaPipe 人像分割 + GrabCut/中心主体降级。"
        ),
    }


def get_birefnet_status(base_dir: Path) -> Dict:
    torch_ready = importlib.util.find_spec("torch") is not None
    transformers_ready = importlib.util.find_spec("transformers") is not None
    pillow_ready = importlib.util.find_spec("PIL") is not None
    runtime_ready = torch_ready and transformers_ready and pillow_ready
    cache_dir = _hf_model_cache_dir(BIREFNET_MODEL_ID)
    local_ready = _has_local_hf_model(BIREFNET_MODEL_ID)
    model_ready = runtime_ready and local_ready
    files = [{
        "path": str(cache_dir),
        "exists": bool(local_ready),
        "size_mb": 0.0,
    }]
    return {
        "ready": model_ready,
        "model_ready": model_ready,
        "runtime_ready": runtime_ready,
        "torch_ready": torch_ready,
        "transformers_ready": transformers_ready,
        "pillow_ready": pillow_ready,
        "local_transformers_ready": local_ready,
        "fallback_ready": True,
        "model_id": BIREFNET_MODEL_ID,
        "backend": "transformers" if model_ready else "fallback",
        "files": files,
        "missing_files": [] if local_ready else [str(cache_dir)],
        "note": (
            "BiRefNet local cache is ready; subject masks will use high-quality matting."
            if model_ready else
            f"BiRefNet local model cache is missing; run: .venv312\\Scripts\\python -c \"from transformers import AutoModelForImageSegmentation; AutoModelForImageSegmentation.from_pretrained('{BIREFNET_MODEL_ID}', trust_remote_code=True); print('OK')\""
        ),
    }


def build_mask_cache_key(
    image_path: str,
    mode: str,
    points: Optional[List[Dict]] = None,
    model_choice: str = "auto",
    version: str = "subject-mask-v1",
) -> str:
    stat_sig = ""
    try:
        st = os.stat(image_path)
        stat_sig = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stat_sig = "missing"
    payload = {
        "version": version,
        "path": str(Path(image_path).resolve()),
        "stat": stat_sig,
        "mode": mode,
        "points": points or [],
        "model": model_choice or "auto",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def load_mask_file(mask_path: str, shape: Tuple[int, int]) -> Optional[np.ndarray]:
    if not mask_path or not os.path.exists(mask_path):
        return None
    arr = np.fromfile(mask_path, dtype=np.uint8)
    if arr.size == 0:
        return None
    mask_u8 = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if mask_u8 is None:
        return None
    h, w = shape[:2]
    if mask_u8.shape[:2] != (h, w):
        mask_u8 = cv2.resize(mask_u8, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(mask_u8.astype(np.float32) / 255.0, 0.0, 1.0)


def save_mask_png(mask: np.ndarray, output_path: str) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    mask_u8 = np.clip(mask * 255.0, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".png", mask_u8)
    if not ok or buf is None:
        raise RuntimeError("mask 编码失败")
    buf.tofile(str(out))


def apply_subject_mask_blend(
    target_img: np.ndarray,
    result_img: np.ndarray,
    mask: np.ndarray,
    mode: str,
    strength: float = 1.0,
) -> np.ndarray:
    if mask is None or mode in ("", "none", None):
        return result_img
    h, w = target_img.shape[:2]
    if mask.shape[:2] != (h, w):
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
    strength = float(np.clip(strength, 0.0, 1.0))
    soft = np.clip(mask.astype(np.float32), 0.0, 1.0)

    if mode in ("protect_subject", "background_only", "protect_local"):
        keep_original = soft * strength
    elif mode in ("subject_only", "local_only"):
        keep_original = (1.0 - soft) * strength
    else:
        return result_img

    keep_original = np.clip(keep_original, 0.0, 1.0)[:, :, np.newaxis]
    mixed = result_img.astype(np.float32) * (1.0 - keep_original) + target_img.astype(np.float32) * keep_original
    return np.clip(mixed, 0, 255).astype(result_img.dtype)


def generate_subject_mask(
    img_bgr: np.ndarray,
    mode: str = "subject",
    points: Optional[List[Dict]] = None,
    prefer_birefnet: bool = True,
    model_choice: str = "auto",
) -> Tuple[np.ndarray, Dict]:
    mode = (mode or "subject").lower()
    model_choice = str(model_choice or "auto").lower()
    points = points or []

    if mode in ("local", "point", "points") and points:
        mask = _grabcut_from_points(img_bgr, points)
        source = "grabcut_points"
    else:
        use_birefnet = prefer_birefnet and model_choice not in ("fallback", "mediapipe", "grabcut", "fast")
        mask, source = _birefnet_subject_mask(img_bgr) if use_birefnet else (None, "birefnet_disabled")
        if mask is None or float(mask.max()) <= 0:
            mask = _person_or_subject_mask(img_bgr)
            source = "mediapipe_person" if float(mask.max()) > 0 else "grabcut_center"
        if float(mask.max()) <= 0:
            mask = _grabcut_center_subject(img_bgr)

    if mask is None or float(mask.max()) <= 0:
        mask = _center_ellipse_mask(img_bgr.shape[:2])
        source = "center_fallback"

    mask = _refine_mask(img_bgr, mask)
    meta = {
        "source": source,
        "model_choice": model_choice,
        "coverage": round(float(np.mean(mask > 0.5)), 4),
        "mode": mode,
        "points": len(points),
    }
    return mask, meta


def _person_or_subject_mask(img_bgr: np.ndarray) -> np.ndarray:
    try:
        from .segmentation import detect_person_region

        person = detect_person_region(img_bgr)
        if person is not None and float(person.max()) > 0:
            return person.astype(np.float32)
    except Exception:
        pass
    return np.zeros(img_bgr.shape[:2], dtype=np.float32)


def _birefnet_subject_mask(img_bgr: np.ndarray) -> Tuple[Optional[np.ndarray], str]:
    runtime = _load_birefnet_transformers()
    if not runtime:
        return None, "birefnet_unavailable"
    try:
        import torch
        from PIL import Image

        h, w = img_bgr.shape[:2]
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        resized = pil.resize((1024, 1024), Image.BILINEAR)
        arr = np.asarray(resized).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = torch.from_numpy(((arr - mean) / std).transpose(2, 0, 1)).unsqueeze(0).to(runtime["device"])
        with torch.no_grad():
            output = runtime["model"](tensor)
        if isinstance(output, (list, tuple)):
            pred = output[-1]
        elif isinstance(output, dict):
            pred = None
            for key in ("logits", "pred", "preds", "saliency"):
                if key in output:
                    pred = output[key]
                    break
            if pred is None:
                pred = next(iter(output.values()))
        else:
            pred = output
        if isinstance(pred, (list, tuple)):
            pred = pred[-1]
        pred = pred.sigmoid().detach().float().cpu().numpy()
        mask = np.squeeze(pred)
        if mask.ndim != 2:
            return None, "birefnet_bad_output"
        mask = cv2.resize(mask.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
        return np.clip(mask, 0.0, 1.0), "birefnet_subject"
    except Exception as exc:
        global BIREFNET_ERROR
        BIREFNET_ERROR = f"{type(exc).__name__}: {exc}"
        return None, "birefnet_error"


def _load_birefnet_transformers():
    global BIREFNET_CACHE, BIREFNET_ERROR
    if BIREFNET_CACHE is not None:
        return BIREFNET_CACHE
    if not _has_local_hf_model(BIREFNET_MODEL_ID):
        BIREFNET_ERROR = "BiRefNet runtime or local model cache is not ready"
        return None
    try:
        import torch
        from transformers import AutoModelForImageSegmentation

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = AutoModelForImageSegmentation.from_pretrained(
            BIREFNET_MODEL_ID,
            trust_remote_code=True,
            local_files_only=True,
        ).to(device).eval()
        BIREFNET_CACHE = {"model": model, "device": device}
        BIREFNET_ERROR = None
        return BIREFNET_CACHE
    except Exception as exc:
        BIREFNET_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def _hf_model_cache_dir(model_id: str) -> Path:
    safe_id = "models--" + model_id.replace("/", "--")
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub" / safe_id


def _has_local_hf_model(model_id: str) -> bool:
    model_dir = _hf_model_cache_dir(model_id)
    if not model_dir.exists():
        return False
    refs_main = model_dir / "refs" / "main"
    snapshots_dir = model_dir / "snapshots"
    candidates = []
    if refs_main.exists():
        try:
            revision = refs_main.read_text(encoding="utf-8").strip()
            if revision:
                candidates.append(snapshots_dir / revision)
        except OSError:
            pass
    if snapshots_dir.exists() and not candidates:
        try:
            candidates.extend(path for path in snapshots_dir.iterdir() if path.is_dir())
        except OSError:
            pass
    for snapshot in candidates[:3]:
        has_config = (snapshot / "config.json").exists()
        has_weights = any((snapshot / name).exists() for name in ("pytorch_model.bin", "model.safetensors"))
        if has_config and has_weights:
            return True
    return False


def _grabcut_center_subject(img_bgr: np.ndarray) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    margin_x = max(8, int(w * 0.08))
    margin_y = max(8, int(h * 0.08))
    rect = (margin_x, margin_y, max(2, w - margin_x * 2), max(2, h - margin_y * 2))
    return _run_grabcut(img_bgr, rect=rect)


def _grabcut_from_points(img_bgr: np.ndarray, points: List[Dict]) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    fg_points = []
    bg_points = []
    for point in points:
        try:
            x = float(point.get("x", 0.5))
            y = float(point.get("y", 0.5))
            label = str(point.get("label", "fg"))
        except Exception:
            continue
        px = int(np.clip(x, 0, 1) * max(w - 1, 1))
        py = int(np.clip(y, 0, 1) * max(h - 1, 1))
        if label == "bg":
            bg_points.append((px, py))
        else:
            fg_points.append((px, py))

    if not fg_points:
        return _grabcut_center_subject(img_bgr)

    xs = [p[0] for p in fg_points]
    ys = [p[1] for p in fg_points]
    pad = max(32, int(max(w, h) * 0.18))
    x0 = max(1, min(xs) - pad)
    y0 = max(1, min(ys) - pad)
    x1 = min(w - 2, max(xs) + pad)
    y1 = min(h - 2, max(ys) + pad)
    rect = (x0, y0, max(2, x1 - x0), max(2, y1 - y0))

    seed = np.full((h, w), cv2.GC_PR_BGD, dtype=np.uint8)
    rx, ry, rw, rh = rect
    seed[ry:ry + rh, rx:rx + rw] = cv2.GC_PR_FGD
    brush = max(6, int(max(w, h) * 0.015))
    for px, py in fg_points:
        cv2.circle(seed, (px, py), brush, cv2.GC_FGD, -1)
    for px, py in bg_points:
        cv2.circle(seed, (px, py), brush, cv2.GC_BGD, -1)
    return _run_grabcut(img_bgr, mask_seed=seed)


def _run_grabcut(img_bgr: np.ndarray, rect=None, mask_seed=None) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    if min(h, w) < 4:
        return np.ones((h, w), dtype=np.float32)
    work_img = img_bgr
    scale = 1.0
    longest = max(h, w)
    if longest > 1024:
        scale = 1024.0 / longest
        work_img = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    wh, ww = work_img.shape[:2]
    if mask_seed is not None:
        mask = cv2.resize(mask_seed, (ww, wh), interpolation=cv2.INTER_NEAREST)
        mode = cv2.GC_INIT_WITH_MASK
        gc_rect = None
    else:
        if rect is None:
            rect = (max(1, int(ww * 0.08)), max(1, int(wh * 0.08)), max(2, int(ww * 0.84)), max(2, int(wh * 0.84)))
        else:
            x, y, rw, rh = rect
            rect = (int(x * scale), int(y * scale), max(2, int(rw * scale)), max(2, int(rh * scale)))
        mask = np.zeros((wh, ww), dtype=np.uint8)
        mode = cv2.GC_INIT_WITH_RECT
        gc_rect = rect

    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(work_img, mask, gc_rect, bgd, fgd, 4, mode)
    except Exception:
        return np.zeros((h, w), dtype=np.float32)
    out = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 1.0, 0.0).astype(np.float32)
    if out.shape[:2] != (h, w):
        out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)
    return out


def _center_ellipse_mask(shape: Tuple[int, int]) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=np.float32)
    center = (w // 2, h // 2)
    axes = (max(2, int(w * 0.34)), max(2, int(h * 0.42)))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 1.0, -1)
    return mask


def _refine_mask(img_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    mask = cv2.resize(mask.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    mask = np.clip(mask, 0.0, 1.0)
    kernel = np.ones((5, 5), np.uint8)
    binary = (mask > 0.45).astype(np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    soft = cv2.GaussianBlur(binary.astype(np.float32), (0, 0), sigmaX=max(2, min(h, w) / 180.0))
    return np.clip(soft, 0.0, 1.0).astype(np.float32)
