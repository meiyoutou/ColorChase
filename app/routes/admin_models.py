import json
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.routes.auth import require_admin
from config import BASE_DIR, MODFLOWS_B0_CHECKPOINT, MODFLOWS_B6_CHECKPOINT, get_neuralpreset_weight_status, get_storage_cache_dir
from models import User

router = APIRouter()

MODEL_MANAGEMENT_PATH = get_storage_cache_dir() / "model_management.json"
DEFAULT_MODEL_KEYS = {"modflows_b6", "modflows_b0", "ai_portrait_neuralpreset", "dncm_lut", "neural_preset"}


class ModelTogglePayload(BaseModel):
    enabled: bool


class ModelDefaultPayload(BaseModel):
    default_model: str


def _default_model_management():
    return {
        "default_model": "modflows_b6",
        "disabled_models": [],
        "benchmarks": {},
        "last_errors": {},
        "updated_at": None,
    }


def _read_model_management():
    data = _default_model_management()
    if MODEL_MANAGEMENT_PATH.exists():
        try:
            loaded = json.loads(MODEL_MANAGEMENT_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    if not isinstance(data.get("disabled_models"), list):
        data["disabled_models"] = []
    if not isinstance(data.get("benchmarks"), dict):
        data["benchmarks"] = {}
    if not isinstance(data.get("last_errors"), dict):
        data["last_errors"] = {}
    return data


def _write_model_management(data):
    current = _default_model_management()
    current.update(data or {})
    current["disabled_models"] = sorted(set(current.get("disabled_models") or []))
    current["updated_at"] = datetime.utcnow().isoformat() + "Z"
    MODEL_MANAGEMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_MANAGEMENT_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return current


def _file_info(path):
    path = Path(path)
    exists = path.exists()
    size_mb = 0.0
    if exists:
        try:
            size_mb = round(path.stat().st_size / 1024 / 1024, 1)
        except OSError:
            size_mb = 0.0
    return {"path": str(path), "exists": exists, "size_mb": size_mb}


def _model_entry(key, name, kind, ready, files, used_by, status=None, note=""):
    missing = [item["path"] for item in files if item.get("required", True) and not item["exists"]]
    return {
        "key": key,
        "name": name,
        "kind": kind,
        "ready": bool(ready),
        "status": status or ("ready" if ready else "missing"),
        "used_by": used_by,
        "files": files,
        "missing_files": missing,
        "note": note,
    }


def _build_status_payload():
    from algorithms.depth_layers import get_depth_anything_status
    from algorithms.semantic_match import get_dinov2_status
    from algorithms.subject_mask import get_birefnet_status, get_subject_mask_status

    neuralpreset_weight_status = get_neuralpreset_weight_status()
    norm_file = _file_info(neuralpreset_weight_status["norm_path"])
    style_file = _file_info(neuralpreset_weight_status["style_path"])
    neuralpreset_best_file = _file_info(BASE_DIR / "weights" / "neuralpreset" / "best.ckpt")
    neuralpreset_norm_file = _file_info(BASE_DIR / "weights" / "neuralpreset" / "norm_stage_best.pth")
    segface_file = _file_info(BASE_DIR / "weights" / "segface" / "swinb_celeba_512_model_299.pt")
    modflows_b6_file = _file_info(MODFLOWS_B6_CHECKPOINT)
    modflows_b0_file = _file_info(MODFLOWS_B0_CHECKPOINT)
    subject_mask_status = get_subject_mask_status(BASE_DIR)
    birefnet_status = get_birefnet_status(BASE_DIR)
    depth_anything_status = get_depth_anything_status(BASE_DIR)
    dinov2_status = get_dinov2_status(BASE_DIR)

    norm_exists = norm_file["exists"]
    style_exists = style_file["exists"]
    modflows_b6_exists = modflows_b6_file["exists"]
    modflows_b0_exists = modflows_b0_file["exists"]
    ai_portrait_ready = neuralpreset_best_file["exists"]
    segface_ready = segface_file["exists"]

    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
            device_label = torch.cuda.get_device_name(0)
        else:
            device = "cpu"
            device_label = "CPU"
    except Exception:
        device = "unknown"
        device_label = "unknown"

    models = [
        _model_entry("modflows_b6", "ModFlows B6", "neural", modflows_b6_exists, [modflows_b6_file], ["modflows", "regional_modflows", "ai_portrait fallback"], note="主 AI 全局追色模型，当前接口默认使用 B6。"),
        _model_entry("modflows_b0", "ModFlows B0", "neural", modflows_b0_exists, [modflows_b0_file], ["modflows_b0"], note="轻量快速追色模型，已接入 AI 全局追色-B0快速模式。"),
        _model_entry("ai_portrait_neuralpreset", "AI Portrait NeuralPreset", "neural", ai_portrait_ready, [neuralpreset_best_file], ["ai_portrait"], note="AI 人像追色的首选全局追色模型；失败时会回退到 ModFlows。"),
        _model_entry("segface", "SegFace", "segmentation", segface_ready, [segface_file], ["ai_portrait"], note="用于皮肤、唇部、头发语义 mask，不直接追色但会影响人像保护质量。"),
        _model_entry("sam_subject_mask", "SAM/SAM2 Subject Mask", "segmentation", subject_mask_status["sam_ready"] or subject_mask_status["fallback_ready"], subject_mask_status["files"], ["subject_protect", "background_only", "local_mask"], status="ready" if subject_mask_status["sam_ready"] else ("fallback" if subject_mask_status["fallback_ready"] else "missing"), note=subject_mask_status["note"]),
        _model_entry("birefnet_subject_mask", "BiRefNet Subject Mask", "segmentation", birefnet_status["ready"], birefnet_status["files"], ["subject_protect", "background_only", "high_quality_matting"], status="ready" if birefnet_status["model_ready"] else "fallback", note=birefnet_status["note"]),
        _model_entry("depth_anything_v2", "Depth Anything V2", "depth", depth_anything_status["ready"], depth_anything_status["files"], ["depth_layers", "depth_preview", "depth_export"], status="ready" if depth_anything_status["model_ready"] else "fallback", note=depth_anything_status["note"]),
        _model_entry("dinov2_semantic_match", "DINOv2 Semantic Match", "semantic", dinov2_status["ready"], dinov2_status["files"], ["semantic_match", "content_aware_color"], status="ready" if dinov2_status["model_ready"] else "fallback", note=dinov2_status["note"]),
        _model_entry("dncm_lut", "DNCM / NeuralPreset LUT", "neural_lut", neuralpreset_weight_status["ready"], [norm_file, style_file], ["dncm_lut", "lut_preset_export"], note="快速/高质量 LUT 链路需要 norm_stage_best.pth 和 style_stage_best.pth 同时存在。"),
        _model_entry("neural_preset", "NeuralPreset", "neural", neuralpreset_weight_status["ready"], [norm_file, style_file], ["neural_preset"], note="全图 NeuralPreset 追色入口；可单独启用、禁用、设为默认，不再跟 DNCM LUT 共用开关。"),
        _model_entry("neuralpreset_training_assets", "NeuralPreset Training Assets", "training", neuralpreset_norm_file["exists"], [neuralpreset_norm_file], ["training", "metrics"], status="partial" if neuralpreset_norm_file["exists"] else "missing", note="训练/评估辅助权重；不等同于 DNCM LUT 的完整双阶段权重。"),
    ]

    ready_count = sum(1 for item in models if item["ready"] and item["status"] == "ready")

    return {
        "device": device,
        "device_label": device_label,
        "models": models,
        "summary": {
            "total": len(models),
            "ready": ready_count,
            "installed_or_partial": sum(1 for item in models if item["ready"]),
            "missing": sum(1 for item in models if not item["ready"]),
            "ready_rate": round(ready_count / max(len(models), 1) * 100, 1),
            "primary_ai_ready": bool(modflows_b6_exists and ai_portrait_ready and segface_ready),
            "dncm_ready": bool(norm_exists and style_exists),
        },
    }


def _model_install_hint(model):
    missing = model.get("missing_files") or []
    if not missing:
        return "权重已检测到。"
    first = Path(missing[0])
    return f"缺少 {len(missing)} 个权重文件，请放到 {first.parent}，文件名保持为 {first.name}。"


def _synthetic_benchmark_images(size=96):
    x = np.linspace(0, 255, size, dtype=np.uint8)
    y = np.linspace(255, 0, size, dtype=np.uint8)
    grid_x = np.tile(x, (size, 1))
    grid_y = np.tile(y[:, None], (1, size))
    target = np.dstack([grid_x, grid_y, np.full_like(grid_x, 96)])
    reference = np.dstack([np.full_like(grid_x, 42), grid_x, grid_y])
    return target, reference


def _run_model_benchmark(model_key):
    from algorithms.color_transfer import transfer_color
    from algorithms.depth_layers import generate_depth_map
    from algorithms.semantic_match import semantic_match_transfer
    from algorithms.subject_mask import generate_subject_mask

    target, reference = _synthetic_benchmark_images()
    start = time.perf_counter()
    output_shape = None
    detail = ""

    if model_key == "modflows_b6":
        from algorithms.modflows import modflows_transfer
        result = modflows_transfer(target, reference, encoder_type="B6", steps=2, strength=0.45)
        output_shape = list(result.shape)
    elif model_key == "modflows_b0":
        from algorithms.modflows import modflows_transfer
        result = modflows_transfer(target, reference, encoder_type="B0", steps=2, strength=0.45)
        output_shape = list(result.shape)
    elif model_key in ("ai_portrait_neuralpreset", "neural_preset"):
        from algorithms.neural_preset import neural_preset_transfer
        result = neural_preset_transfer(target, reference)
        output_shape = list(result.shape)
    elif model_key == "dncm_lut":
        from algorithms.dncm import generate_lut_from_dncm
        lut = generate_lut_from_dncm(reference, target, 9)
        output_shape = list(lut.shape)
    elif model_key in ("segface", "sam_subject_mask"):
        mask, meta = generate_subject_mask(target, mode="subject", prefer_birefnet=False)
        output_shape = list(mask.shape)
        detail = meta.get("source", "")
    elif model_key == "birefnet_subject_mask":
        mask, meta = generate_subject_mask(target, mode="subject", prefer_birefnet=True)
        output_shape = list(mask.shape)
        detail = meta.get("source", "")
    elif model_key == "depth_anything_v2":
        depth, meta = generate_depth_map(target, BASE_DIR)
        output_shape = list(depth.shape)
        detail = meta.get("source", "")
    elif model_key == "dinov2_semantic_match":
        result, meta = semantic_match_transfer(target, transfer_color(target, reference), reference)
        output_shape = list(result.shape)
        detail = meta.get("source", "")
    elif model_key == "neuralpreset_training_assets":
        detail = "training asset file check"
        output_shape = [0]
    else:
        raise ValueError("未知模型，无法 benchmark")

    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    return {
        "ok": True,
        "elapsed_ms": elapsed_ms,
        "output_shape": output_shape,
        "detail": detail,
        "finished_at": datetime.utcnow().isoformat() + "Z",
    }


def _merge_model_management(status_payload):
    state = _read_model_management()
    disabled = set(state.get("disabled_models") or [])
    benchmarks = state.get("benchmarks") or {}
    errors = state.get("last_errors") or {}
    for model in status_payload.get("models", []):
        key = model.get("key")
        model["enabled"] = key not in disabled
        model["is_default"] = key == state.get("default_model")
        model["default_selectable"] = key in DEFAULT_MODEL_KEYS
        model["benchmark"] = benchmarks.get(key)
        model["last_error"] = errors.get(key)
        model["install_hint"] = _model_install_hint(model)
    return {
        **status_payload,
        "management": {
            "default_model": state.get("default_model"),
            "disabled_models": sorted(disabled),
            "updated_at": state.get("updated_at"),
        },
    }


@router.get("/models")
async def admin_models(_admin: User = Depends(require_admin)):
    return _merge_model_management(_build_status_payload())


@router.post("/models/default")
async def admin_set_default_model(payload: ModelDefaultPayload, _admin: User = Depends(require_admin)):
    status_payload = _build_status_payload()
    models = {item["key"]: item for item in status_payload.get("models", [])}
    default_model = str(payload.default_model or "").strip()
    if default_model not in models:
        return {"success": False, "message": "模型不存在", **_merge_model_management(status_payload)}
    if default_model not in DEFAULT_MODEL_KEYS:
        return {"success": False, "message": "该模型不能作为默认追色模型", **_merge_model_management(status_payload)}
    if not models[default_model].get("ready"):
        return {"success": False, "message": "该模型尚未就绪，不能设为默认", **_merge_model_management(status_payload)}

    state = _read_model_management()
    disabled = set(state.get("disabled_models") or [])
    disabled.discard(default_model)
    state["default_model"] = default_model
    state["disabled_models"] = sorted(disabled)
    _write_model_management(state)
    return {"success": True, "message": "默认模型已更新", **_merge_model_management(_build_status_payload())}


@router.post("/models/{model_key}/toggle")
async def admin_toggle_model(model_key: str, payload: ModelTogglePayload, _admin: User = Depends(require_admin)):
    status_payload = _build_status_payload()
    known_keys = {item["key"] for item in status_payload.get("models", [])}
    if model_key not in known_keys:
        return {"success": False, "message": "模型不存在", **_merge_model_management(status_payload)}

    state = _read_model_management()
    disabled = set(state.get("disabled_models") or [])
    if payload.enabled:
        disabled.discard(model_key)
    else:
        disabled.add(model_key)
        if state.get("default_model") == model_key:
            for item in status_payload.get("models", []):
                key = item["key"]
                if key != model_key and item.get("ready") and key not in disabled and key in DEFAULT_MODEL_KEYS:
                    state["default_model"] = key
                    break
    state["disabled_models"] = sorted(disabled)
    _write_model_management(state)
    return {"success": True, "message": "模型启用状态已更新", **_merge_model_management(_build_status_payload())}


@router.post("/models/{model_key}/benchmark")
async def admin_benchmark_model(model_key: str, _admin: User = Depends(require_admin)):
    status_payload = _build_status_payload()
    models = {item["key"]: item for item in status_payload.get("models", [])}
    if model_key not in models:
        return {"success": False, "message": "模型不存在", **_merge_model_management(status_payload)}

    state = _read_model_management()
    model = models[model_key]
    try:
        if not model.get("ready"):
            raise FileNotFoundError(_model_install_hint(model))
        result = _run_model_benchmark(model_key)
        state.setdefault("benchmarks", {})[model_key] = result
        state.setdefault("last_errors", {}).pop(model_key, None)
        _write_model_management(state)
        merged = _merge_model_management(_build_status_payload())
        return {"success": True, "message": "benchmark 已完成", "result": result, **merged}
    except Exception as exc:
        error_payload = {
            "message": f"{type(exc).__name__}: {exc}",
            "happened_at": datetime.utcnow().isoformat() + "Z",
        }
        state.setdefault("last_errors", {})[model_key] = error_payload
        _write_model_management(state)
        merged = _merge_model_management(_build_status_payload())
        return {"success": False, "message": "benchmark 失败", "error": error_payload, **merged}
