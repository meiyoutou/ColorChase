import os
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.routes.auth import require_admin


def create_model_status_router(
    *,
    base_dir: Path,
    modflows_b6_checkpoint,
    modflows_b0_checkpoint,
    get_neuralpreset_weight_status,
    get_subject_mask_status,
    get_birefnet_status,
    get_depth_anything_status,
    get_dinov2_status,
    merge_public_model_management,
):
    router = APIRouter()

    @router.get("/api/model_status")
    async def api_model_status(admin=Depends(require_admin)):
        def file_info(path):
            path_str = str(path)
            exists = os.path.exists(path_str)
            size_mb = 0.0
            if exists:
                try:
                    size_mb = round(os.path.getsize(path_str) / (1024 * 1024), 1)
                except OSError:
                    size_mb = 0.0
            return {
                "path": path_str,
                "exists": exists,
                "size_mb": size_mb,
            }

        def model_entry(key, name, kind, ready, files, used_by, status=None, note=""):
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

        neuralpreset_weight_status = get_neuralpreset_weight_status()
        norm_file = file_info(neuralpreset_weight_status["norm_path"])
        style_file = file_info(neuralpreset_weight_status["style_path"])
        neuralpreset_best_file = file_info(base_dir / "weights" / "neuralpreset" / "best.ckpt")
        neuralpreset_norm_file = file_info(base_dir / "weights" / "neuralpreset" / "norm_stage_best.pth")
        segface_file = file_info(base_dir / "weights" / "segface" / "swinb_celeba_512_model_299.pt")
        modflows_b6_file = file_info(modflows_b6_checkpoint)
        modflows_b0_file = file_info(modflows_b0_checkpoint)
        subject_mask_status = get_subject_mask_status(base_dir)
        birefnet_status = get_birefnet_status(base_dir)
        depth_anything_status = get_depth_anything_status(base_dir)
        dinov2_status = get_dinov2_status(base_dir)

        norm_exists = norm_file["exists"]
        style_exists = style_file["exists"]
        modflows_b6_exists = modflows_b6_file["exists"]
        modflows_b0_exists = modflows_b0_file["exists"]
        ai_portrait_ready = neuralpreset_best_file["exists"]
        segface_ready = segface_file["exists"]

        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "unknown"

        models = [
            model_entry(
                "modflows_b6",
                "ModFlows B6",
                "neural",
                modflows_b6_exists,
                [modflows_b6_file],
                ["modflows", "regional_modflows", "ai_portrait fallback"],
                note="主 AI 全局追色模型，当前接口默认使用 B6。",
            ),
            model_entry(
                "modflows_b0",
                "ModFlows B0",
                "neural",
                modflows_b0_exists,
                [modflows_b0_file],
                ["modflows_b0"],
                note="轻量快速追色模型，已接入 AI 全局追色-B0快速模式。",
            ),
            model_entry(
                "ai_portrait_neuralpreset",
                "AI Portrait NeuralPreset",
                "neural",
                ai_portrait_ready,
                [neuralpreset_best_file],
                ["ai_portrait"],
                note="AI 人像追色的首选全局追色模型；失败时会回退到 ModFlows。",
            ),
            model_entry(
                "dncm_lut",
                "DNCM / NeuralPreset LUT",
                "neural_lut",
                neuralpreset_weight_status["ready"],
                [norm_file, style_file],
                ["dncm_lut", "lut_preset_export"],
                note="快速/高质量 LUT 链路需要 norm_stage_best.pth 和 style_stage_best.pth 同时存在。",
            ),
            model_entry(
                "neural_preset",
                "NeuralPreset",
                "neural",
                neuralpreset_weight_status["ready"],
                [norm_file, style_file],
                ["neural_preset"],
                note="全图 NeuralPreset 追色入口；可单独启用、禁用、设为默认，不再跟 DNCM LUT 共用开关。",
            ),
            model_entry(
                "segface",
                "SegFace",
                "segmentation",
                segface_ready,
                [segface_file],
                ["ai_portrait"],
                note="用于皮肤、唇部、头发语义 mask，不直接追色但会影响人像保护质量。",
            ),
            model_entry(
                "sam_subject_mask",
                "SAM/SAM2 Subject Mask",
                "segmentation",
                subject_mask_status["sam_ready"] or subject_mask_status["fallback_ready"],
                subject_mask_status["files"],
                ["subject_protect", "background_only", "local_mask"],
                status="ready" if subject_mask_status["sam_ready"] else ("fallback" if subject_mask_status["fallback_ready"] else "missing"),
                note=subject_mask_status["note"],
            ),
            model_entry(
                "birefnet_subject_mask",
                "BiRefNet Subject Mask",
                "segmentation",
                birefnet_status["ready"],
                birefnet_status["files"],
                ["subject_protect", "background_only", "high_quality_matting"],
                status="ready" if birefnet_status["model_ready"] else "fallback",
                note=birefnet_status["note"],
            ),
            model_entry(
                "depth_anything_v2",
                "Depth Anything V2",
                "depth",
                depth_anything_status["ready"],
                depth_anything_status["files"],
                ["depth_layers", "depth_preview", "depth_export"],
                status="ready" if depth_anything_status["model_ready"] else "fallback",
                note=depth_anything_status["note"],
            ),
            model_entry(
                "dinov2_semantic_match",
                "DINOv2 Semantic Match",
                "semantic",
                dinov2_status["ready"],
                dinov2_status["files"],
                ["semantic_match", "content_aware_color"],
                status="ready" if dinov2_status["model_ready"] else "fallback",
                note=dinov2_status["note"],
            ),
            model_entry(
                "neuralpreset_training_assets",
                "NeuralPreset Training Assets",
                "training",
                neuralpreset_norm_file["exists"],
                [neuralpreset_norm_file],
                ["training", "metrics"],
                status="partial" if neuralpreset_norm_file["exists"] else "missing",
                note="训练/评估辅助权重；不等同于 DNCM LUT 的完整双阶段权重。",
            ),
        ]

        payload = {
            "norm_stage_trained": norm_exists,
            "style_stage_trained": style_exists,
            "neural_preset_ready": neuralpreset_weight_status["ready"],
            "neuralpreset_weight_dirs": neuralpreset_weight_status["model_dirs"],
            "neuralpreset_missing_weights": neuralpreset_weight_status["missing"],
            "modflows_b6_ready": modflows_b6_exists,
            "modflows_b0_ready": modflows_b0_exists,
            "modflows_ready": modflows_b6_exists or modflows_b0_exists,
            "ai_portrait_ready": ai_portrait_ready,
            "segface_ready": segface_ready,
            "sam_subject_ready": subject_mask_status["sam_ready"],
            "subject_mask_ready": subject_mask_status["ready"],
            "subject_mask_fallback_ready": subject_mask_status["fallback_ready"],
            "birefnet_subject_ready": birefnet_status["model_ready"],
            "birefnet_subject_runtime_ready": birefnet_status["runtime_ready"],
            "depth_anything_ready": depth_anything_status["model_ready"],
            "depth_layers_ready": depth_anything_status["ready"],
            "depth_layers_fallback_ready": depth_anything_status["fallback_ready"],
            "dinov2_ready": dinov2_status["model_ready"],
            "semantic_match_ready": dinov2_status["ready"],
            "semantic_match_fallback_ready": dinov2_status["fallback_ready"],
            "device": device,
            "models": models,
            "summary": {
                "total": len(models),
                "ready": sum(1 for item in models if item["ready"] and item["status"] == "ready"),
                "installed_or_partial": sum(1 for item in models if item["ready"]),
                "missing": sum(1 for item in models if not item["ready"]),
                "ready_rate": round(
                    sum(1 for item in models if item["ready"] and item["status"] == "ready")
                    / max(len(models), 1)
                    * 100,
                    1,
                ),
                "primary_ai_ready": bool(modflows_b6_exists and ai_portrait_ready and segface_ready),
                "dncm_ready": bool(norm_exists and style_exists),
            },
        }
        return JSONResponse(merge_public_model_management(payload))

    return router
