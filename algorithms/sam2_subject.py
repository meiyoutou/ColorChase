import importlib.util
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


SAM2_MODEL_CANDIDATES = (
    ("weights/sam2/sam2_hiera_small.pt", "sam2_hiera_s", "sam2_small"),
    ("weights/sam2/sam2_hiera_base_plus.pt", "sam2_hiera_b+", "sam2_base_plus"),
    ("weights/sam2/sam2_hiera_tiny.pt", "sam2_hiera_t", "sam2_tiny"),
    ("models/sam2/sam2_hiera_small.pt", "sam2_hiera_s", "sam2_small"),
    ("models/sam2/sam2_hiera_base_plus.pt", "sam2_hiera_b+", "sam2_base_plus"),
    ("models/sam2/sam2_hiera_tiny.pt", "sam2_hiera_t", "sam2_tiny"),
)

_SAM2_CACHE = None
_SAM2_CACHE_LOCK = threading.RLock()
_SAM2_LAST_ERROR = None


def _sam2_runtime_ready() -> bool:
    return (
        importlib.util.find_spec("sam2") is not None
        and importlib.util.find_spec("torch") is not None
    )


def _select_device(torch_module):
    if torch_module.cuda.is_available():
        return torch_module.device("cuda")
    mps = getattr(getattr(torch_module, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        return torch_module.device("mps")
    return torch_module.device("cpu")


def _find_weight(base_dir: Path, preferred: str = "sam2_small") -> Optional[Tuple[Path, str, str]]:
    preferred = str(preferred or "sam2").lower()
    candidates = list(SAM2_MODEL_CANDIDATES)
    if preferred in ("sam2_small", "small"):
        candidates.sort(key=lambda item: 0 if item[2] == "sam2_small" else 1)
    elif preferred in ("sam2_base_plus", "base_plus", "base+"):
        candidates.sort(key=lambda item: 0 if item[2] == "sam2_base_plus" else 1)
    elif preferred in ("sam2_tiny", "tiny"):
        candidates.sort(key=lambda item: 0 if item[2] == "sam2_tiny" else 1)
    for rel_path, config_name, model_name in candidates:
        path = base_dir / rel_path
        if path.is_file():
            return path, config_name, model_name
    return None


def get_sam2_status(base_dir: Path) -> Dict:
    files = []
    for rel_path, config_name, model_name in SAM2_MODEL_CANDIDATES:
        path = base_dir / rel_path
        files.append(
            {
                "path": str(path),
                "exists": path.is_file(),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1)
                if path.is_file()
                else 0.0,
                "model": model_name,
                "config": config_name,
                "required": False,
            }
        )

    runtime_ready = _sam2_runtime_ready()
    device_name = "unknown"
    if runtime_ready:
        try:
            import torch

            device_name = str(_select_device(torch))
        except Exception:
            device_name = "unknown"
    selected = _find_weight(base_dir)
    ready = bool(runtime_ready and selected)
    return {
        "ready": ready,
        "runtime_ready": runtime_ready,
        "device": device_name,
        "selected_model": selected[2] if selected else None,
        "selected_path": str(selected[0]) if selected else None,
        "files": files,
        "last_error": _SAM2_LAST_ERROR,
        "note": (
            "SAM2 Small will be used for subject masks."
            if ready and selected[2] == "sam2_small"
            else "SAM2 subject-mask runtime is ready."
            if ready
            else "SAM2 runtime or weights are missing."
        ),
    }


def clear_sam2_runtime_cache() -> None:
    global _SAM2_CACHE, _SAM2_LAST_ERROR
    with _SAM2_CACHE_LOCK:
        _SAM2_CACHE = None
        _SAM2_LAST_ERROR = None


def _load_sam2(base_dir: Path, model_choice: str):
    global _SAM2_CACHE, _SAM2_LAST_ERROR
    with _SAM2_CACHE_LOCK:
        selected = _find_weight(base_dir, model_choice)
        if not selected:
            raise RuntimeError("SAM2 weight file is missing")
        weight_path, config_name, model_name = selected

        import torch

        device = _select_device(torch)
        cache_key = (str(weight_path.resolve()), str(device))
        if _SAM2_CACHE and _SAM2_CACHE.get("key") == cache_key:
            return _SAM2_CACHE

        try:
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor

            model = build_sam2(
                config_name,
                str(weight_path),
                device=str(device),
                mode="eval",
            )
            predictor = SAM2ImagePredictor(model)
            _SAM2_CACHE = {
                "key": cache_key,
                "model": model,
                "predictor": predictor,
                "device": device,
                "model_name": model_name,
                "weight_path": str(weight_path),
            }
            _SAM2_LAST_ERROR = None
            return _SAM2_CACHE
        except Exception as exc:
            _SAM2_LAST_ERROR = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"SAM2 load failed: {exc}") from exc


def _auto_prompt_box(image_bgr: np.ndarray) -> np.ndarray:
    height, width = image_bgr.shape[:2]
    try:
        from .segmentation import detect_person_region

        person_mask = detect_person_region(image_bgr)
        ys, xs = np.where(person_mask > 0.35)
        if len(xs) >= 32:
            pad_x = max(8, int(width * 0.04))
            pad_y = max(8, int(height * 0.04))
            x0 = max(0, int(xs.min()) - pad_x)
            y0 = max(0, int(ys.min()) - pad_y)
            x1 = min(width - 1, int(xs.max()) + pad_x)
            y1 = min(height - 1, int(ys.max()) + pad_y)
            return np.array([x0, y0, x1, y1], dtype=np.float32)
    except Exception:
        pass

    margin_x = max(1, int(width * 0.08))
    margin_y = max(1, int(height * 0.08))
    return np.array(
        [margin_x, margin_y, width - margin_x - 1, height - margin_y - 1],
        dtype=np.float32,
    )


def predict_sam2_subject_mask(
    image_bgr: np.ndarray,
    points: Optional[List[Dict]] = None,
    base_dir: Optional[Path] = None,
    model_choice: str = "sam2_small",
) -> Tuple[np.ndarray, Dict]:
    if image_bgr is None or image_bgr.ndim != 3:
        raise ValueError("invalid image for SAM2")
    if base_dir is None:
        base_dir = Path(__file__).resolve().parents[1]
    points = points or []

    runtime = _load_sam2(base_dir, model_choice)
    predictor = runtime["predictor"]
    height, width = image_bgr.shape[:2]
    point_coords = []
    point_labels = []
    for point in points:
        try:
            x = float(point.get("x", 0.5))
            y = float(point.get("y", 0.5))
            label = 0 if str(point.get("label", "fg")).lower() == "bg" else 1
        except Exception:
            continue
        point_coords.append(
            [
                float(np.clip(x, 0.0, 1.0) * max(width - 1, 1)),
                float(np.clip(y, 0.0, 1.0) * max(height - 1, 1)),
            ]
        )
        point_labels.append(label)

    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    box = _auto_prompt_box(image_bgr)
    with _SAM2_CACHE_LOCK:
        try:
            import torch

            with torch.inference_mode():
                predictor.set_image(rgb)
                masks, scores, _ = predictor.predict(
                    point_coords=np.asarray(point_coords, dtype=np.float32)
                    if point_coords
                    else None,
                    point_labels=np.asarray(point_labels, dtype=np.int32)
                    if point_labels
                    else None,
                    box=box,
                    multimask_output=bool(point_coords),
                    return_logits=False,
                )
            score_values = np.asarray(scores).reshape(-1)
            mask_values = np.asarray(masks)
            if mask_values.ndim == 2:
                best_mask = mask_values
            else:
                best_index = int(np.argmax(score_values)) if len(score_values) else 0
                best_mask = mask_values[best_index]
            mask = np.clip(best_mask.astype(np.float32), 0.0, 1.0)
            meta = {
                "source": "sam2",
                "model_choice": runtime["model_name"],
                "device": str(runtime["device"]),
                "prompt": "points+box" if point_coords else "person_box",
                "points": len(point_coords),
                "score": round(float(score_values.max()), 4)
                if len(score_values)
                else None,
                "coverage": round(float(np.mean(mask > 0.5)), 4),
            }
            return mask, meta
        except Exception as exc:
            global _SAM2_LAST_ERROR
            _SAM2_LAST_ERROR = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"SAM2 inference failed: {exc}") from exc
        finally:
            try:
                predictor.reset_predictor()
            except Exception:
                pass
