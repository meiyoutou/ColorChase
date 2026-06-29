import json

from fastapi import HTTPException

from algorithms.depth_layers import get_depth_anything_status
from algorithms.semantic_match import get_dinov2_status
from algorithms.subject_mask import get_birefnet_status, get_subject_mask_status
from config import BASE_DIR, STORAGE_CACHE_DIR

MODEL_MANAGEMENT_PATH = STORAGE_CACHE_DIR / "model_management.json"


def _load_model_management_runtime():
    data = {
        "default_model": "modflows_b6",
        "disabled_models": [],
        "benchmarks": {},
        "last_errors": {},
        "updated_at": None,
    }
    if MODEL_MANAGEMENT_PATH.exists():
        try:
            loaded = json.loads(MODEL_MANAGEMENT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception as exc:
            print(f"[Model Management] read config failed: {exc}")
    if not isinstance(data.get("disabled_models"), list):
        data["disabled_models"] = []
    if not isinstance(data.get("benchmarks"), dict):
        data["benchmarks"] = {}
    if not isinstance(data.get("last_errors"), dict):
        data["last_errors"] = {}
    return data


def _merge_public_model_management(status_payload: dict) -> dict:
    state = _load_model_management_runtime()
    disabled = set(state.get("disabled_models") or [])
    benchmarks = state.get("benchmarks") or {}
    errors = state.get("last_errors") or {}
    default_model = state.get("default_model")
    for model in status_payload.get("models", []):
        key = model.get("key")
        model["enabled"] = key not in disabled
        model["is_default"] = key == default_model
        model["benchmark"] = benchmarks.get(key)
        model["last_error"] = errors.get(key)
    status_payload["management"] = {
        "default_model": default_model,
        "disabled_models": sorted(disabled),
        "updated_at": state.get("updated_at"),
    }
    return status_payload


def _model_key_to_algorithm(model_key: str) -> str:
    return {
        "modflows_b6": "modflows",
        "modflows_b0": "modflows_b0",
        "ai_portrait_neuralpreset": "ai_portrait",
        "dncm_lut": "dncm_lut",
        "neural_preset": "neural_preset",
    }.get(model_key or "", "modflows")


def _resolve_transfer_model_runtime(algorithm: str):
    state = _load_model_management_runtime()
    disabled = set(state.get("disabled_models") or [])
    default_model = str(state.get("default_model") or "modflows_b6")
    resolved_algorithm = str(algorithm or "luminance_partition")
    if resolved_algorithm in ("default", "auto", "model_default"):
        resolved_algorithm = _model_key_to_algorithm(default_model)

    modflows_key = default_model if default_model in ("modflows_b0", "modflows_b6") else "modflows_b6"
    modflows_encoder = "B0" if modflows_key == "modflows_b0" else "B6"
    direct_model_key = {
        "modflows": modflows_key,
        "modflows_b0": "modflows_b0",
        "regional_modflows": modflows_key,
        "dncm_lut": "dncm_lut",
        "neural_preset": "neural_preset",
    }.get(resolved_algorithm)
    return {
        "algorithm": resolved_algorithm,
        "disabled": disabled,
        "default_model": default_model,
        "model_key": direct_model_key,
        "modflows_key": modflows_key,
        "modflows_encoder": modflows_encoder,
        "ai_portrait_neural_enabled": "ai_portrait_neuralpreset" not in disabled,
        "modflows_enabled": modflows_key not in disabled,
        "modflows_b0_enabled": "modflows_b0" not in disabled,
        "segface_enabled": "segface" not in disabled,
        "semantic_match_enabled": "dinov2_semantic_match" not in disabled,
        "depth_layers_enabled": "depth_anything_v2" not in disabled,
        "subject_mask_enabled": "sam_subject_mask" not in disabled,
        "birefnet_subject_enabled": "birefnet_subject_mask" not in disabled,
    }


def _disabled_model_error(model_key: str) -> str:
    return f"当前模型已在后台禁用：{model_key}"


def _force_model_ready(model_key: str, ready: bool, label: str):
    disabled = set(_load_model_management_runtime().get("disabled_models") or [])
    if model_key in disabled:
        raise HTTPException(status_code=400, detail=_disabled_model_error(model_key))
    if not ready:
        raise HTTPException(status_code=400, detail=f"{label} 当前不可用，请在模型管理中检查权重和运行库")


def _resolve_mask_model_choice(mask_model: str) -> dict:
    choice = str(mask_model or "auto").strip().lower()
    disabled = set(_load_model_management_runtime().get("disabled_models") or [])
    subject_status = get_subject_mask_status(BASE_DIR)
    birefnet_status = get_birefnet_status(BASE_DIR)

    if choice in ("fallback", "mediapipe", "grabcut", "fast"):
        return {
            "choice": "fallback",
            "model_key": "subject_mask_fallback",
            "prefer_birefnet": False,
            "note": "using fast fallback mask",
        }
    if choice in ("sam", "sam2", "sam_subject_mask"):
        _force_model_ready("sam_subject_mask", bool(subject_status.get("sam_ready")), "SAM/SAM2 主体分割")
        raise HTTPException(status_code=400, detail="SAM/SAM2 权重已检测到，但当前后端还没有接入真实 SAM 推理，暂不能强制选择")
    if choice in ("birefnet", "birefnet_subject_mask"):
        _force_model_ready("birefnet_subject_mask", bool(birefnet_status.get("model_ready")), "BiRefNet 主体抠图")
        return {
            "choice": "birefnet",
            "model_key": "birefnet_subject_mask",
            "prefer_birefnet": True,
            "note": "using BiRefNet",
        }
    if choice not in ("", "auto"):
        raise HTTPException(status_code=400, detail=f"不支持的 mask 模型: {mask_model}")

    if "birefnet_subject_mask" not in disabled and birefnet_status.get("model_ready"):
        return {
            "choice": "birefnet",
            "model_key": "birefnet_subject_mask",
            "prefer_birefnet": True,
            "note": "auto selected BiRefNet",
        }
    return {
        "choice": "fallback",
        "model_key": "subject_mask_fallback",
        "prefer_birefnet": False,
        "note": "auto selected fallback mask",
    }


def _resolve_depth_model_choice(depth_model: str) -> dict:
    choice = str(depth_model or "auto").strip().lower()
    status = get_depth_anything_status(BASE_DIR)
    disabled = set(_load_model_management_runtime().get("disabled_models") or [])
    if choice in ("fallback", "heuristic", "fast"):
        return {"choice": "fallback", "model_key": "depth_fallback"}
    if choice in ("depth_anything_v2", "dav2"):
        _force_model_ready("depth_anything_v2", bool(status.get("model_ready")), "Depth Anything V2")
        return {"choice": "depth_anything_v2", "model_key": "depth_anything_v2"}
    if choice not in ("", "auto"):
        raise HTTPException(status_code=400, detail=f"不支持的深度模型: {depth_model}")
    if "depth_anything_v2" not in disabled and status.get("model_ready"):
        return {"choice": "depth_anything_v2", "model_key": "depth_anything_v2"}
    return {"choice": "fallback", "model_key": "depth_fallback"}


def _resolve_semantic_model_choice(semantic_model: str) -> dict:
    choice = str(semantic_model or "auto").strip().lower()
    status = get_dinov2_status(BASE_DIR)
    disabled = set(_load_model_management_runtime().get("disabled_models") or [])
    if choice in ("fallback", "rules", "hsv", "fast"):
        return {"choice": "fallback", "model_key": "semantic_fallback"}
    if choice in ("dinov2", "dinov2_semantic_match"):
        _force_model_ready("dinov2_semantic_match", bool(status.get("model_ready")), "DINOv2 语义匹配")
        return {"choice": "dinov2", "model_key": "dinov2_semantic_match"}
    if choice not in ("", "auto"):
        raise HTTPException(status_code=400, detail=f"不支持的语义模型: {semantic_model}")
    if "dinov2_semantic_match" not in disabled and status.get("model_ready"):
        return {"choice": "dinov2", "model_key": "dinov2_semantic_match"}
    return {"choice": "fallback", "model_key": "semantic_fallback"}
