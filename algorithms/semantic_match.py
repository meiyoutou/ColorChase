import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np


DINOV2_MODEL_ID = "facebook/dinov2-small"
DINOV2_WEIGHT_CANDIDATES = (
    "weights/dinov2/dinov2_vits14.pth",
    "weights/dinov2/dinov2_vitb14.pth",
    "weights/dinov2/dinov2_vitl14.pth",
    "models/dinov2/dinov2_vits14.pth",
    "models/dinov2/dinov2_vitb14.pth",
    "models/dinov2/dinov2_vitl14.pth",
)
_DINOV2_CACHE = None
_DINOV2_ERROR = None


def get_dinov2_status(base_dir: Path) -> Dict:
    files = []
    for rel_path in DINOV2_WEIGHT_CANDIDATES:
        path = base_dir / rel_path
        files.append({
            "path": str(path),
            "exists": path.exists(),
            "size_mb": round(path.stat().st_size / (1024 * 1024), 1) if path.exists() else 0.0,
        })

    has_weight = any(item["exists"] for item in files)
    torch_ready = importlib.util.find_spec("torch") is not None
    dinov2_package_ready = importlib.util.find_spec("dinov2") is not None
    transformers_ready = importlib.util.find_spec("transformers") is not None
    local_transformers_ready = _has_local_transformers_dinov2()
    if local_transformers_ready:
        cache_dir = _dinov2_transformers_cache_dir()
        files.append({
            "path": str(cache_dir),
            "exists": True,
            "size_mb": _path_size_mb(cache_dir),
            "required": False,
        })
    for index, item in enumerate(files):
        item["required"] = (not has_weight and not local_transformers_ready and index == 0)
    runtime_ready = torch_ready and (dinov2_package_ready or transformers_ready)
    model_ready = runtime_ready and (has_weight or local_transformers_ready)
    backend = "transformers" if transformers_ready else ("dinov2" if dinov2_package_ready else "fallback")

    return {
        "ready": True,
        "model_ready": model_ready,
        "runtime_ready": runtime_ready,
        "torch_ready": torch_ready,
        "transformers_ready": transformers_ready,
        "dinov2_package_ready": dinov2_package_ready,
        "local_transformers_ready": local_transformers_ready,
        "backend": backend if model_ready else "fallback",
        "model_id": DINOV2_MODEL_ID,
        "fallback_ready": True,
        "files": files,
        "missing_files": [item["path"] for item in files if item.get("required", True) and not item["exists"]],
        "note": (
            "DINOv2 runtime is ready; semantic matching will use DINOv2 features."
            if model_ready else
            f"DINOv2 runtime or local model cache is missing; run: .venv312\\Scripts\\python -c \"from transformers import AutoModel; AutoModel.from_pretrained('{DINOV2_MODEL_ID}'); print('OK')\""
        ),
    }


def build_semantic_cache_key(
    target_path: str,
    reference_path: str,
    model_choice: str = "auto",
    version: str = "semantic-match-v1",
) -> str:
    def _sig(path: str) -> str:
        try:
            st = os.stat(path)
            return f"{Path(path).resolve()}:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            return f"{path}:missing"

    raw = json.dumps({
        "version": version,
        "target": _sig(target_path),
        "reference": _sig(reference_path),
        "model": model_choice or "auto",
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def semantic_match_transfer(
    target_img: np.ndarray,
    result_img: np.ndarray,
    reference_img: np.ndarray,
    strength: float = 0.55,
    model_choice: str = "auto",
) -> Tuple[np.ndarray, Dict]:
    dinov2_meta = None
    model_choice = str(model_choice or "auto").lower()
    if model_choice not in ("fallback", "rules", "hsv", "fast"):
        try:
            dinov2_meta = _dinov2_region_match_meta(target_img, reference_img)
        except Exception as exc:
            dinov2_meta = {"source": "dinov2_error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        dinov2_meta = {"source": "dinov2_disabled_by_choice"}
    if dinov2_meta and dinov2_meta.get("source") == "dinov2_transformers":
        return _apply_semantic_matches(target_img, result_img, reference_img, strength, dinov2_meta["matches"], dinov2_meta)
    return _fallback_semantic_match_transfer(target_img, result_img, reference_img, strength, dinov2_meta)


def _fallback_semantic_match_transfer(
    target_img: np.ndarray,
    result_img: np.ndarray,
    reference_img: np.ndarray,
    strength: float = 0.55,
    fallback_reason: Optional[Dict] = None,
) -> Tuple[np.ndarray, Dict]:
    strength = float(np.clip(strength, 0.0, 1.0))
    target_regions = build_semantic_regions(target_img)
    reference_regions = build_semantic_regions(reference_img)
    target_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    result_lab = cv2.cvtColor(result_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    reference_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB).astype(np.float32)

    output_lab = result_lab.copy()
    matches = []
    for region_name, target_mask in target_regions.items():
        if _mask_coverage(target_mask) < 0.01:
            continue
        ref_name, score = _best_reference_region(region_name, target_mask, target_img, reference_regions, reference_img)
        ref_mask = reference_regions.get(ref_name)
        if ref_mask is None or _mask_coverage(ref_mask) < 0.01:
            continue
        corrected = _match_lab_stats(result_lab, reference_lab, target_mask, ref_mask)
        region_strength = _region_strength(region_name, strength, score)
        soft = _soften_mask(target_img, target_mask)[:, :, np.newaxis]
        output_lab = output_lab * (1.0 - soft * region_strength) + corrected * (soft * region_strength)
        matches.append({
            "target": region_name,
            "reference": ref_name,
            "score": round(float(score), 4),
            "coverage": round(float(_mask_coverage(target_mask)), 4),
            "strength": round(float(region_strength), 4),
        })

    output = cv2.cvtColor(np.clip(output_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    meta = {
        "source": "semantic_region_fallback",
        "matches": matches,
        "match_count": len(matches),
    }
    if fallback_reason:
        meta["fallback_reason"] = fallback_reason
    return output, meta


def build_semantic_regions(img_bgr: np.ndarray) -> Dict[str, np.ndarray]:
    from .segmentation import detect_person_region, detect_skin_region, detect_sky_region

    h, w = img_bgr.shape[:2]
    sky = _safe_mask(lambda: detect_sky_region(img_bgr), (h, w))
    skin = _safe_mask(lambda: detect_skin_region(img_bgr), (h, w))
    person = _safe_mask(lambda: detect_person_region(img_bgr), (h, w))
    person = np.clip(np.maximum(person, skin), 0.0, 1.0)

    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    saturation = hsv[:, :, 1] / 255.0
    value = hsv[:, :, 2] / 255.0
    green = (((hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 85)) & (saturation > 0.22) & (value > 0.18)).astype(np.float32)
    warm = (((hsv[:, :, 0] < 30) | (hsv[:, :, 0] > 165)) & (saturation > 0.20) & (value > 0.18)).astype(np.float32)

    known = np.clip(np.maximum.reduce([sky, person, green * 0.55, warm * 0.35]), 0.0, 1.0)
    background = np.clip(1.0 - known, 0.0, 1.0)

    regions = {
        "sky": sky,
        "skin": skin,
        "person": person,
        "greenery": _refine_region(green),
        "warm_object": _refine_region(warm),
        "background": _refine_region(background),
    }
    return {name: np.clip(mask.astype(np.float32), 0.0, 1.0) for name, mask in regions.items()}


def summarize_semantic_matches(
    target_img: np.ndarray,
    reference_img: np.ndarray,
    model_choice: str = "auto",
) -> Dict:
    model_choice = str(model_choice or "auto").lower()
    if model_choice not in ("fallback", "rules", "hsv", "fast"):
        try:
            dinov2_meta = _dinov2_region_match_meta(target_img, reference_img)
            if dinov2_meta.get("source") == "dinov2_transformers":
                dinov2_meta["model_choice"] = model_choice
                return dinov2_meta
        except Exception as exc:
            dinov2_meta = {"source": "dinov2_error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        dinov2_meta = {"source": "dinov2_disabled_by_choice"}
    target_regions = build_semantic_regions(target_img)
    reference_regions = build_semantic_regions(reference_img)
    matches = []
    for region_name, target_mask in target_regions.items():
        if _mask_coverage(target_mask) < 0.01:
            continue
        ref_name, score = _best_reference_region(region_name, target_mask, target_img, reference_regions, reference_img)
        matches.append({
            "target": region_name,
            "reference": ref_name,
            "score": round(float(score), 4),
            "coverage": round(float(_mask_coverage(target_mask)), 4),
        })
    return {
        "source": "semantic_region_fallback",
        "model_choice": model_choice,
        "matches": matches,
        "match_count": len(matches),
        "fallback_reason": dinov2_meta,
    }


def _has_local_transformers_dinov2() -> bool:
    model_dir = _dinov2_transformers_cache_dir()
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
        has_weights = (snapshot / "pytorch_model.bin").exists() or (snapshot / "model.safetensors").exists()
        if has_config and has_weights:
            return True
    return False


def _dinov2_transformers_cache_dir() -> Path:
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub" / "models--facebook--dinov2-small"


def _path_size_mb(path: Path) -> float:
    try:
        if path.is_file():
            return round(path.stat().st_size / (1024 * 1024), 1)
        total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        return round(total / (1024 * 1024), 1)
    except OSError:
        return 0.0


def _load_dinov2_transformers():
    global _DINOV2_CACHE, _DINOV2_ERROR
    if _DINOV2_CACHE is not None:
        return _DINOV2_CACHE
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("transformers") is None:
        _DINOV2_ERROR = "torch or transformers is not installed"
        return None
    try:
        import torch
        from transformers import AutoModel

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = AutoModel.from_pretrained(DINOV2_MODEL_ID, local_files_only=True).to(device).eval()
        _DINOV2_CACHE = {"model": model, "device": device}
        _DINOV2_ERROR = None
        return _DINOV2_CACHE
    except Exception as exc:
        _DINOV2_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def _dinov2_region_match_meta(target_img: np.ndarray, reference_img: np.ndarray) -> Dict:
    runtime = _load_dinov2_transformers()
    if not runtime:
        return {"source": "dinov2_unavailable", "error": _DINOV2_ERROR}

    target_regions = build_semantic_regions(target_img)
    reference_regions = build_semantic_regions(reference_img)
    matches = []
    reference_features = {}

    for region_name, target_mask in target_regions.items():
        if _mask_coverage(target_mask) < 0.01:
            continue
        target_desc = _dinov2_region_descriptor(target_img, target_mask, runtime)
        best_name = None
        best_score = -1.0
        for ref_name, ref_mask in reference_regions.items():
            if _mask_coverage(ref_mask) < 0.01:
                continue
            if ref_name not in reference_features:
                reference_features[ref_name] = _dinov2_region_descriptor(reference_img, ref_mask, runtime)
            ref_desc = reference_features[ref_name]
            score = 0.55 * _cosine_similarity(target_desc, ref_desc) + 0.45 * _name_prior(region_name, ref_name)
            if score > best_score:
                best_name = ref_name
                best_score = score
        if best_name:
            matches.append({
                "target": region_name,
                "reference": best_name,
                "score": round(float(np.clip(best_score, 0.0, 1.0)), 4),
                "coverage": round(float(_mask_coverage(target_mask)), 4),
            })

    return {
        "source": "dinov2_transformers",
        "backend": "transformers",
        "model_id": DINOV2_MODEL_ID,
        "matches": matches,
        "match_count": len(matches),
    }


def _dinov2_image_feature(img_bgr: np.ndarray, runtime: Dict) -> np.ndarray:
    import torch

    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    normalized = (resized - mean) / std
    pixel_values = torch.from_numpy(normalized.transpose(2, 0, 1)).unsqueeze(0).to(runtime["device"])
    with torch.no_grad():
        outputs = runtime["model"](pixel_values=pixel_values)
    pooled = getattr(outputs, "pooler_output", None)
    if pooled is None:
        pooled = outputs.last_hidden_state[:, 0]
    feature = pooled.detach().float().cpu().numpy()[0]
    norm = np.linalg.norm(feature)
    return feature / max(float(norm), 1e-6)


def _dinov2_region_descriptor(img_bgr: np.ndarray, mask: np.ndarray, runtime: Dict) -> np.ndarray:
    crop = _crop_masked_region(img_bgr, mask)
    image_feature = _dinov2_image_feature(crop, runtime)
    handcrafted = _region_descriptor(img_bgr, mask)
    return np.concatenate([image_feature.astype(np.float32), handcrafted.astype(np.float32)], axis=0)


def _crop_masked_region(img_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape[:2]
    binary = mask > 0.35
    if not np.any(binary):
        return img_bgr
    ys, xs = np.where(binary)
    y0, y1 = int(max(0, ys.min() - 8)), int(min(h, ys.max() + 9))
    x0, x1 = int(max(0, xs.min() - 8)), int(min(w, xs.max() + 9))
    crop = img_bgr[y0:y1, x0:x1].copy()
    crop_mask = binary[y0:y1, x0:x1]
    if crop.size == 0:
        return img_bgr
    fill = np.array(cv2.mean(img_bgr)[:3], dtype=np.uint8)
    crop[~crop_mask] = fill
    return crop


def _apply_semantic_matches(target_img, result_img, reference_img, strength, matches, source_meta):
    strength = float(np.clip(strength, 0.0, 1.0))
    target_regions = build_semantic_regions(target_img)
    reference_regions = build_semantic_regions(reference_img)
    result_lab = cv2.cvtColor(result_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    reference_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    output_lab = result_lab.copy()
    applied = []

    for match in matches:
        region_name = match.get("target")
        ref_name = match.get("reference")
        target_mask = target_regions.get(region_name)
        ref_mask = reference_regions.get(ref_name)
        if target_mask is None or ref_mask is None:
            continue
        if _mask_coverage(target_mask) < 0.01 or _mask_coverage(ref_mask) < 0.01:
            continue
        score = float(match.get("score", 0.5))
        corrected = _match_lab_stats(result_lab, reference_lab, target_mask, ref_mask)
        region_strength = _region_strength(region_name, strength, score)
        soft = _soften_mask(target_img, target_mask)[:, :, np.newaxis]
        output_lab = output_lab * (1.0 - soft * region_strength) + corrected * (soft * region_strength)
        applied.append({**match, "strength": round(float(region_strength), 4)})

    output = cv2.cvtColor(np.clip(output_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    meta = dict(source_meta)
    meta["matches"] = applied
    meta["match_count"] = len(applied)
    return output, meta


def _safe_mask(factory, shape: Tuple[int, int]) -> np.ndarray:
    try:
        mask = factory()
        if mask is not None:
            return cv2.resize(mask.astype(np.float32), (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
    except Exception:
        pass
    return np.zeros(shape, dtype=np.float32)


def _best_reference_region(region_name, target_mask, target_img, reference_regions, reference_img):
    preferred = {
        "sky": ("sky", "background"),
        "skin": ("skin", "person", "warm_object"),
        "person": ("person", "skin", "warm_object", "background"),
        "greenery": ("greenery", "background"),
        "warm_object": ("warm_object", "skin", "person"),
        "background": ("background", "sky", "greenery"),
    }
    target_desc = _region_descriptor(target_img, target_mask)
    best_name = None
    best_score = -1.0
    for candidate in preferred.get(region_name, tuple(reference_regions.keys())):
        ref_mask = reference_regions.get(candidate)
        if ref_mask is None or _mask_coverage(ref_mask) < 0.01:
            continue
        ref_desc = _region_descriptor(reference_img, ref_mask)
        score = 0.65 * _name_prior(region_name, candidate) + 0.35 * _descriptor_similarity(target_desc, ref_desc)
        if score > best_score:
            best_name = candidate
            best_score = score
    if best_name is None:
        best_name = "background"
        best_score = 0.2
    return best_name, best_score


def _region_descriptor(img_bgr, mask):
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    weights = np.clip(mask.astype(np.float32), 0.0, 1.0)
    total = float(weights.sum())
    if total < 1e-6:
        return np.zeros(6, dtype=np.float32)
    desc = [
        _weighted_mean(hsv[:, :, 0] / 180.0, weights, total),
        _weighted_mean(hsv[:, :, 1] / 255.0, weights, total),
        _weighted_mean(hsv[:, :, 2] / 255.0, weights, total),
        _weighted_mean(lab[:, :, 0] / 255.0, weights, total),
        _weighted_mean(lab[:, :, 1] / 255.0, weights, total),
        _weighted_mean(lab[:, :, 2] / 255.0, weights, total),
    ]
    return np.array(desc, dtype=np.float32)


def _weighted_mean(channel, weights, total):
    return float((channel * weights).sum() / max(total, 1e-6))


def _descriptor_similarity(a, b):
    dist = float(np.linalg.norm(a - b))
    return float(np.clip(1.0 - dist / 1.75, 0.0, 1.0))


def _cosine_similarity(a, b):
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-6:
        return 0.0
    return float(np.clip(np.dot(a, b) / denom, 0.0, 1.0))


def _name_prior(target_name, reference_name):
    if target_name == reference_name:
        return 1.0
    pairs = {
        ("skin", "person"),
        ("person", "skin"),
        ("sky", "background"),
        ("background", "sky"),
        ("warm_object", "skin"),
        ("warm_object", "person"),
    }
    return 0.65 if (target_name, reference_name) in pairs else 0.35


def _match_lab_stats(result_lab, reference_lab, target_mask, reference_mask):
    corrected = result_lab.copy()
    target_weights = np.clip(target_mask.astype(np.float32), 0.0, 1.0)
    reference_weights = np.clip(reference_mask.astype(np.float32), 0.0, 1.0)
    for ch in (1, 2):
        t_vals = result_lab[:, :, ch]
        r_vals = reference_lab[:, :, ch]
        t_mean, t_std = _weighted_stats(t_vals, target_weights)
        r_mean, r_std = _weighted_stats(r_vals, reference_weights)
        gain = np.clip(r_std / max(t_std, 1e-4), 0.55, 1.65)
        corrected[:, :, ch] = (t_vals - t_mean) * gain + r_mean
    return np.clip(corrected, 0, 255)


def _weighted_stats(values, weights):
    total = float(weights.sum())
    if total < 1e-6:
        return float(values.mean()), float(values.std() + 1e-4)
    mean = float((values * weights).sum() / total)
    var = float((((values - mean) ** 2) * weights).sum() / total)
    return mean, float(np.sqrt(max(var, 1e-4)))


def _region_strength(region_name, base_strength, score):
    multipliers = {
        "sky": 1.08,
        "background": 1.0,
        "greenery": 0.9,
        "warm_object": 0.82,
        "person": 0.58,
        "skin": 0.42,
    }
    return float(np.clip(base_strength * multipliers.get(region_name, 0.8) * (0.55 + 0.45 * score), 0.0, 0.85))


def _soften_mask(img_bgr, mask):
    h, w = mask.shape[:2]
    mask = _refine_region(mask)
    sigma = max(2.0, min(h, w) / 180.0)
    mask = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigmaX=sigma)
    try:
        guide = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        mask_u8 = np.clip(mask * 255, 0, 255).astype(np.uint8)
        refined = cv2.ximgproc.guidedFilter(guide, mask_u8, radius=8, eps=1e-2)
        mask = refined.astype(np.float32) / 255.0
    except Exception:
        pass
    return np.clip(mask, 0.0, 1.0)


def _refine_region(mask):
    mask = np.clip(mask.astype(np.float32), 0.0, 1.0)
    binary = (mask > 0.35).astype(np.uint8)
    kernel = np.ones((5, 5), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    return binary.astype(np.float32)


def _mask_coverage(mask):
    return float(np.mean(mask > 0.35))
