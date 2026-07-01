import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.security import DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES, ensure_upload_file_size, get_upload_file_size
from app.settings import int_env
from config import get_training_corpus_dir


def _append_upload_index(training_path: Path, records: list):
    """把上传记录追加到 training_path/upload_index.json
    结构: {"samples": [{file, original_name, relative_path, group, uploaded_at}, ...]}
    用于训练样本页按子文件夹分组展示。图片仍按 UUID 平铺保存，不强依赖真实目录结构。
    """
    index_path = training_path / "upload_index.json"
    data = {"samples": []}
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("samples"), list):
                    data = loaded
        except (ValueError, OSError):
            pass
    data["samples"].extend(records)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_training_router(
    *,
    progress_manager,
    get_request_user_id,
    get_request_user_role,
    write_task_log,
    get_training_data_stats_payload,
    resolve_training_dir,
    training_image_extensions,
    run_training_task,
    neuralpreset_model_dir,
):
    router = APIRouter()

    @router.post("/api/train")
    async def api_train(
        target: str = Form("neuralpreset"),
        stage: str = Form("both"),
        image_dir: str = Form(...),
        epochs: int = Form(100),
        batch_size: int = Form(4),
        lr: float = Form(1e-4),
        authorization: Optional[str] = Header(None),
    ):
        request_user_id = get_request_user_id(authorization)
        request_user_role = get_request_user_role(authorization)
        if target not in ("neuralpreset", "modflows_b0", "modflows_b6"):
            raise HTTPException(status_code=400, detail="不支持的训练目标")

        stats = get_training_data_stats_payload(image_dir)
        training_path = Path(stats["image_dir"])
        file_count = stats["training_file_count"]
        size_mb = stats["training_size_mb"]
        if file_count == 0:
            raise HTTPException(status_code=400, detail="训练目录中没有可用图片")

        if target in ("modflows_b0", "modflows_b6"):
            target_label = "ModFlows B0" if target == "modflows_b0" else "ModFlows B6"
            write_task_log(
                task_id="",
                task_type="模型训练",
                event_type="request",
                status="fail",
                summary=target_label + " 训练未开放",
                detail=target_label + " 训练链路当前尚未实现，现阶段仅支持 NeuralPreset",
                user_id=request_user_id,
                role=request_user_role,
                model=target,
                meta={"stage": stage, "epochs": epochs, "batch_size": batch_size, "lr": lr, "training_file_count": file_count},
            )
            raise HTTPException(status_code=501, detail=target_label + " 训练链路当前尚未实现，现阶段仅支持 NeuralPreset")

        task_id = progress_manager.create_task()
        write_task_log(
            task_id=task_id,
            task_type="模型训练",
            event_type="request",
            status="info",
            summary="训练任务已启动",
            detail=f"目标: {target}，阶段: {stage}",
            user_id=request_user_id,
            role=request_user_role,
            model=target,
            meta={"stage": stage, "epochs": epochs, "batch_size": batch_size, "lr": lr, "training_file_count": file_count, "training_size_mb": size_mb},
        )
        asyncio.create_task(run_training_task(task_id, stage, str(training_path), epochs, batch_size, lr, request_user_id, request_user_role, target))
        return JSONResponse({
            "success": True,
            "task_id": task_id,
            "target": target,
            "training_file_count": file_count,
            "training_size_mb": size_mb,
            "model_dir": str(neuralpreset_model_dir),
        })

    @router.post("/api/train/upload")
    async def api_train_upload(
        files: List[UploadFile] = File(...),
        image_dir: str = Form(str(get_training_corpus_dir())),
        relative_paths: str = Form(""),
    ):
        training_path = resolve_training_dir(image_dir)
        training_path.mkdir(parents=True, exist_ok=True)
        # 解析前端传来的相对路径数组（来自 webkitRelativePath）
        # 选文件按钮上传时这个字段为空，group 统一为 ""（根目录）
        try:
            rel_paths_list = json.loads(relative_paths) if relative_paths else []
        except (ValueError, TypeError):
            rel_paths_list = []
        if not isinstance(rel_paths_list, list):
            rel_paths_list = []

        saved = []
        skipped = 0
        skipped_unsupported = 0
        skipped_too_large = 0
        index_records = []
        now_iso = datetime.now(timezone.utc).isoformat()
        max_training_image_bytes = int_env(
            "COLORCHASE_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES",
            DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES,
        )
        for idx, file in enumerate(files):
            if not file or not file.filename:
                skipped += 1
                continue
            ext = Path(file.filename).suffix.lower() or ".jpg"
            if ext not in training_image_extensions:
                skipped += 1
                skipped_unsupported += 1
                continue
            size = get_upload_file_size(file)
            if size is not None and size > max_training_image_bytes:
                skipped += 1
                skipped_too_large += 1
                continue
            ensure_upload_file_size(file, max_training_image_bytes, label="训练图片")
            save_name = f"{uuid.uuid4().hex}{ext}"
            save_path = training_path / save_name
            content = await file.read()
            with open(save_path, "wb") as f:
                f.write(content)
            saved.append(save_name)

            # 提取相对路径和分组（用于训练样本页按子文件夹展示）
            rel_path = ""
            if idx < len(rel_paths_list) and rel_paths_list[idx]:
                rel_path = str(rel_paths_list[idx])
            if not rel_path:
                rel_path = file.filename or ""
            # group = webkitRelativePath 中第一级子目录名（跳过根文件夹名）
            # 例: "MyDataset/sunsets/a.jpg" -> "sunsets"
            #     "MyDataset/a.jpg" -> ""（根目录）
            parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
            group = parts[1] if len(parts) >= 2 else ""

            index_records.append({
                "file": save_name,
                "original_name": file.filename or "",
                "relative_path": rel_path,
                "group": group,
                "uploaded_at": now_iso,
            })

        # 保存分组信息到 upload_index.json（追加模式），图片仍平铺在 training_path 根目录
        if index_records:
            _append_upload_index(training_path, index_records)

        stats = get_training_data_stats_payload(str(training_path))
        return JSONResponse({
            "success": True,
            "saved_count": len(saved),
            "skipped_count": skipped,
            "skipped_unsupported_count": skipped_unsupported,
            "skipped_too_large_count": skipped_too_large,
            "training_file_count": stats["training_file_count"],
            "training_size_mb": stats["training_size_mb"],
            "image_dir": stats["image_dir"],
        })

    @router.get("/api/train/data_stats")
    async def api_train_data_stats(image_dir: str = str(get_training_corpus_dir())):
        return JSONResponse({
            "success": True,
            **get_training_data_stats_payload(image_dir),
        })

    @router.post("/api/train/clear_uploads")
    @router.post("/api/train/data_clear", include_in_schema=False)
    async def api_train_clear_uploads(image_dir: str = Form(str(get_training_corpus_dir()))):
        training_path = resolve_training_dir(image_dir)
        deleted = 0
        if training_path.exists() and training_path.is_dir():
            for path in training_path.rglob("*"):
                if path.is_file() and path.suffix.lower() in training_image_extensions:
                    path.unlink()
                    deleted += 1
            index_path = training_path / "upload_index.json"
            if index_path.exists() and index_path.is_file():
                index_path.unlink()

        stats = get_training_data_stats_payload(str(training_path))
        return JSONResponse({
            "success": True,
            "deleted_count": deleted,
            "training_file_count": stats["training_file_count"],
            "training_size_mb": stats["training_size_mb"],
            "image_dir": stats["image_dir"],
        })

    return router
