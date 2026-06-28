import asyncio
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.security import ensure_upload_file_size
from config import get_training_corpus_dir


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
    ):
        training_path = resolve_training_dir(image_dir)
        training_path.mkdir(parents=True, exist_ok=True)
        saved = []
        for file in files:
            if not file or not file.filename:
                continue
            ext = Path(file.filename).suffix.lower() or ".jpg"
            if ext not in training_image_extensions:
                continue
            ensure_upload_file_size(file, 10 * 1024 * 1024, label="训练图片")
            save_name = f"{uuid.uuid4().hex}{ext}"
            save_path = training_path / save_name
            content = await file.read()
            with open(save_path, "wb") as f:
                f.write(content)
            saved.append(save_name)

        stats = get_training_data_stats_payload(str(training_path))
        return JSONResponse({
            "success": True,
            "saved_count": len(saved),
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

    return router
