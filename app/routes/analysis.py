import asyncio
import json
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.security import ensure_upload_file_size


def create_analysis_router(
    base_dir,
    resolve_depth_model_choice,
    resolve_semantic_model_choice,
    resolve_mask_model_choice,
    resolve_local_file_path,
    runtime_depth_dir,
    runtime_mask_dir,
    cv2_imread,
    generate_depth_map,
    save_depth_png,
    build_depth_cache_key,
    generate_subject_mask,
    save_mask_png,
    build_mask_cache_key,
    build_semantic_cache_key,
    summarize_semantic_matches,
    save_upload,
):
    router = APIRouter()

    @router.post("/api/depth/layers")
    async def api_depth_layers(
        target_path: str = Form(...),
        depth_model: str = Form("auto"),
    ):
        depth_choice = resolve_depth_model_choice(depth_model)
        resolved_target_path = resolve_local_file_path(target_path)
        if not resolved_target_path:
            raise HTTPException(status_code=400, detail="目标图片不存在")
        target_path = str(resolved_target_path)

        cache_key = build_depth_cache_key(target_path, depth_choice["choice"])
        depth_dir = runtime_depth_dir()
        depth_path = depth_dir / f"{cache_key}_depth.png"
        meta_path = depth_dir / f"{cache_key}_depth.json"
        cached = depth_path.exists()

        if cached:
            meta = {}
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        else:
            target_img = await asyncio.to_thread(cv2_imread, target_path, target_size=1536)
            if target_img is None:
                raise HTTPException(status_code=400, detail="无法读取目标图片")
            depth, meta = await asyncio.to_thread(generate_depth_map, target_img, base_dir, depth_choice["choice"])
            await asyncio.to_thread(save_depth_png, depth, str(depth_path))
            meta.update({
                "cache_key": cache_key,
                "width": int(target_img.shape[1]),
                "height": int(target_img.shape[0]),
            })
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        token = int(time.time() * 1000)
        return JSONResponse({
            "success": True,
            "depth_id": cache_key,
            "depth_path": str(depth_path),
            "depth_url": f"/temp_luts/depth/{depth_path.name}?t={token}",
            "cached": cached,
            "model": depth_choice,
            "meta": meta,
        })

    @router.post("/api/semantic/match")
    async def api_semantic_match(
        target_path: str = Form(...),
        reference_path: str = Form(None),
        reference: UploadFile = File(None),
        semantic_model: str = Form("auto"),
    ):
        semantic_choice = resolve_semantic_model_choice(semantic_model)
        resolved_target_path = resolve_local_file_path(target_path)
        if not resolved_target_path:
            raise HTTPException(status_code=400, detail="目标图片不存在")
        target_path = str(resolved_target_path)
        if reference is not None and reference.filename:
            ensure_upload_file_size(reference, 10 * 1024 * 1024, label="参考图")
            reference_path = await asyncio.to_thread(save_upload, reference)
        resolved_reference_path = resolve_local_file_path(reference_path)
        if not resolved_reference_path:
            raise HTTPException(status_code=400, detail="参考图片不存在")
        reference_path = str(resolved_reference_path)

        cache_key = build_semantic_cache_key(target_path, reference_path, semantic_choice["choice"])
        target_img = await asyncio.to_thread(cv2_imread, target_path, target_size=1536)
        reference_img = await asyncio.to_thread(cv2_imread, reference_path, target_size=1536)
        if target_img is None or reference_img is None:
            raise HTTPException(status_code=400, detail="无法读取目标图或参考图")
        meta = await asyncio.to_thread(summarize_semantic_matches, target_img, reference_img, semantic_choice["choice"])
        return JSONResponse({
            "success": True,
            "semantic_id": cache_key,
            "reference_path": reference_path,
            "model": semantic_choice,
            "meta": meta,
        })

    @router.post("/api/mask/subject")
    async def api_subject_mask(
        target_path: str = Form(...),
        mode: str = Form("subject"),
        points_json: str = Form("[]"),
        mask_model: str = Form("auto"),
    ):
        mask_choice = resolve_mask_model_choice(mask_model)
        prefer_birefnet = bool(mask_choice.get("prefer_birefnet"))
        resolved_target_path = resolve_local_file_path(target_path)
        if not resolved_target_path:
            raise HTTPException(status_code=400, detail="目标图片不存在")
        target_path = str(resolved_target_path)
        try:
            points = json.loads(points_json or "[]")
            if not isinstance(points, list):
                points = []
        except Exception:
            points = []

        cache_key = build_mask_cache_key(target_path, mode, points, mask_choice["choice"])
        mask_dir = runtime_mask_dir()
        mask_path = mask_dir / f"{cache_key}_mask.png"
        meta_path = mask_dir / f"{cache_key}_mask.json"
        cached = mask_path.exists()

        if cached:
            meta = {}
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        else:
            target_img = await asyncio.to_thread(cv2_imread, target_path, target_size=1536)
            if target_img is None:
                raise HTTPException(status_code=400, detail="无法读取目标图片")
            mask, meta = await asyncio.to_thread(
                generate_subject_mask,
                target_img,
                mode,
                points,
                prefer_birefnet,
                mask_choice["choice"],
            )
            await asyncio.to_thread(save_mask_png, mask, str(mask_path))
            meta.update({
                "cache_key": cache_key,
                "width": int(target_img.shape[1]),
                "height": int(target_img.shape[0]),
            })
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        token = int(time.time() * 1000)
        return JSONResponse({
            "success": True,
            "mask_id": cache_key,
            "mask_path": str(mask_path),
            "mask_url": f"/temp_luts/masks/{mask_path.name}?t={token}",
            "cached": cached,
            "model": mask_choice,
            "meta": meta,
        })

    return router
