import asyncio
import base64
import io
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.security import DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES, ensure_upload_file_size, get_upload_file_size
from app.services.paths import _is_admin_request, _training_corpus_dir_for_label, _user_assets_root_for_label
from app.services.user_identity import resolve_user_storage_label
from app.settings import int_env
from config import get_training_corpus_dir
from core.io.loaders.universal_loader import RAW_EXTS, is_supported, load_image_bgr


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
        # 训练任务使用服务器语料库，仅管理员可启动
        if request_user_role != "admin":
            raise HTTPException(status_code=403, detail="模型训练仅限管理员")
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
        authorization: Optional[str] = Header(None),
    ):
        # 训练语料库属于服务器数据，仅管理员可上传；普通用户数据不落盘
        if not _is_admin_request(authorization):
            raise HTTPException(status_code=403, detail="训练语料上传仅限管理员")

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
    async def api_train_data_stats(
        image_dir: str = str(get_training_corpus_dir()),
        authorization: Optional[str] = Header(None),
    ):
        if not _is_admin_request(authorization):
            raise HTTPException(status_code=403, detail="训练数据统计仅限管理员")
        return JSONResponse({
            "success": True,
            **get_training_data_stats_payload(image_dir),
        })

    @router.post("/api/detection/upload")
    async def api_detection_upload(
        file: UploadFile = File(...),
        file_uuid: str = Form(""),
        authorization: Optional[str] = Header(None),
    ):
        """用户上传原图时自动保存一份检测库副本，按用户邮箱隔离，持久化保存。"""
        request_user_id = get_request_user_id(authorization)
        if request_user_id is None:
            raise HTTPException(status_code=401, detail="请先登录")

        storage_label = await resolve_user_storage_label(request_user_id)
        detection_dir = _user_assets_root_for_label(storage_label) / "detection"
        safe_uuid = _safe_filename(str(file_uuid or uuid.uuid4().hex))
        target_dir = detection_dir / safe_uuid
        target_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(file.filename or "").suffix.lower() or ".jpg"
        save_path = target_dir / f"original{ext}"

        max_bytes = int_env(
            "COLORCHASE_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES",
            DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES,
        )
        ensure_upload_file_size(file, max_bytes, label="检测库原图")
        content = await file.read()
        with open(save_path, "wb") as f:
            f.write(content)

        return JSONResponse({
            "success": True,
            "file_uuid": safe_uuid,
            "saved_path": str(save_path.relative_to(get_training_corpus_dir().parent.parent)),
        })

    @router.post("/api/training/upload")
    async def api_training_upload(
        target: UploadFile = File(...),
        result: UploadFile = File(...),
        reference: Optional[UploadFile] = File(None),
        lut: Optional[UploadFile] = File(None),
        meta: str = Form("{}"),
        sample_uuid: str = Form(""),
        is_video: str = Form("0"),
        authorization: Optional[str] = Header(None),
    ):
        """追色结果自动入库，所有登录用户导出时都可保存训练样本，按用户邮箱隔离。"""
        request_user_id = get_request_user_id(authorization)
        if request_user_id is None:
            raise HTTPException(status_code=401, detail="请先登录")

        storage_label = await resolve_user_storage_label(request_user_id)
        safe_uuid = _safe_filename(str(sample_uuid or uuid.uuid4().hex))
        sample_dir = _training_corpus_dir_for_label(storage_label) / safe_uuid
        sample_dir.mkdir(parents=True, exist_ok=True)

        max_bytes = int_env(
            "COLORCHASE_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES",
            DEFAULT_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES,
        )

        saved = {}
        files_map = {
            "target": target,
            "result": result,
        }
        if reference is not None:
            files_map["reference"] = reference
        if lut is not None:
            files_map["lut"] = lut

        for key, upload_file in files_map.items():
            if upload_file is None or not upload_file.filename:
                continue
            ext = Path(upload_file.filename).suffix.lower() or ".jpg"
            if key == "lut":
                ext = ".cube"
            save_path = sample_dir / f"{key}{ext}"
            ensure_upload_file_size(upload_file, max_bytes, label=f"训练样本 {key}")
            content = await upload_file.read()
            with open(save_path, "wb") as f:
                f.write(content)
            saved[key] = str(save_path.name)

        meta_path = sample_dir / "meta.json"
        try:
            meta_obj = json.loads(meta) if meta else {}
        except (ValueError, TypeError):
            meta_obj = {}
        if not isinstance(meta_obj, dict):
            meta_obj = {}
        meta_obj.update({
            "sample_uuid": safe_uuid,
            "user_id": request_user_id,
            "storage_label": storage_label,
            "is_video": is_video == "1" or is_video.lower() == "true",
            "saved_files": saved,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_obj, f, ensure_ascii=False, indent=2)

        return JSONResponse({
            "success": True,
            "sample_uuid": safe_uuid,
            "saved_files": saved,
        })

    @router.get("/api/train/samples")
    async def api_train_samples(
        authorization: Optional[str] = Header(None),
    ):
        """列出所有训练样本元数据，仅管理员可用。"""
        if not _is_admin_request(authorization):
            raise HTTPException(status_code=403, detail="训练样本列表仅限管理员")

        corpus_root = get_training_corpus_dir()
        samples = []
        users_set = set()

        if corpus_root.exists() and corpus_root.is_dir():
            for user_dir in sorted(corpus_root.iterdir()):
                if not user_dir.is_dir():
                    continue
                if user_dir.name.startswith("."):
                    continue
                label = user_dir.name
                # 先检查这个用户目录下有没有样本子目录，没有就跳过，避免空目录混进筛选列表
                sample_subdirs = [d for d in user_dir.iterdir() if d.is_dir()]
                if not sample_subdirs:
                    continue
                users_set.add(label)
                for sample_dir in sorted(sample_subdirs):
                    if not sample_dir.is_dir():
                        continue
                    meta_path = sample_dir / "meta.json"
                    meta_obj = {}
                    if meta_path.exists():
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta_obj = json.load(f)
                                if not isinstance(meta_obj, dict):
                                    meta_obj = {}
                        except (ValueError, OSError):
                            meta_obj = {}

                    actual_files = {}
                    for item in sample_dir.iterdir():
                        if item.is_file():
                            actual_files[item.name] = item.stat().st_size

                    has_reference = any(k.startswith("reference") for k in actual_files)
                    has_lut = any(k.startswith("lut") or k.endswith(".cube") for k in actual_files)

                    # 统计可用于训练的原图数量（DNCM/NeuralPreset 训练只需要 target）
                    # 包括标准图片和 RAW 相机格式
                    target_count = sum(
                        1 for k in actual_files
                        if k.lower().startswith("target") and (
                            k.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")) or
                            k.lower().endswith(tuple(RAW_EXTS))
                        )
                    )

                    samples.append({
                        "sample_uuid": sample_dir.name,
                        "storage_label": label,
                        "user_id": meta_obj.get("user_id"),
                        "algorithm": meta_obj.get("algorithm", ""),
                        "rating": meta_obj.get("rating", 0),
                        "format": meta_obj.get("format", ""),
                        "is_video": meta_obj.get("is_video", False),
                        "uploaded_at": meta_obj.get("uploaded_at", ""),
                        "has_reference": has_reference,
                        "has_lut": has_lut,
                        "file_count": len(actual_files),
                        "target_count": target_count,
                        "files": {k: v for k, v in actual_files.items()},
                    })

        return JSONResponse({
            "success": True,
            "total": len(samples),
            "users": sorted(users_set),
            "samples": samples,
        })

    @router.get("/api/train/samples/{sample_uuid}/preview")
    async def api_train_sample_preview(
        sample_uuid: str,
        storage_label: str = "",
        authorization: Optional[str] = Header(None),
    ):
        """预览单个训练样本，返回缩略图 base64，仅管理员可用。"""
        if not _is_admin_request(authorization):
            raise HTTPException(status_code=403, detail="训练样本预览仅限管理员")

        safe_uuid = _safe_filename(sample_uuid)
        sample_dir = _resolve_training_sample_dir(safe_uuid, storage_label)

        meta_obj = {}
        meta_path = sample_dir / "meta.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta_obj = json.load(f)
                    if not isinstance(meta_obj, dict):
                        meta_obj = {}
            except (ValueError, OSError):
                meta_obj = {}

        # 清理 meta 里的旧文件路径字段，避免乱码路径干扰前端展示
        _path_keys = {"target_path", "files", "reference_path", "result_path", "lut_path"}
        meta_clean = {k: v for k, v in meta_obj.items() if k not in _path_keys}

        images = {}
        lut_content = None
        file_list = []
        for item in sorted(sample_dir.iterdir()):
            if not item.is_file():
                continue
            file_list.append(item.name)
            name_lower = item.name.lower()

            # 目标 / 参考 / 结果图统一走通用加载器，既支持 JPG/PNG 等常规格式，也支持 RAW。
            # 之前分两套逻辑，RAW 分支还做了 BGR->RGB 多余转换，可能导致颜色异常；这里直接加载 BGR 并编码。
            if name_lower.startswith(("target", "reference", "result")):
                # 跳过缩略图缓存文件，避免 key 冲突覆盖真正的原图
                if name_lower.endswith(".thumb.jpg"):
                    continue
                try:
                    import cv2
                    if not is_supported(str(item)):
                        continue
                    bgr, _ = load_image_bgr(str(item), target_size=2048, mode="preview")
                    if bgr is None or bgr.size == 0:
                        continue
                    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                        key = name_lower.split(".")[0]
                        images[key] = f"data:image/jpeg;base64,{b64}"
                except Exception as exc:
                    print(f"[Training Preview] 解码失败 {item.name}: {exc}")
                    continue
            elif name_lower.endswith(".cube") or name_lower.endswith(".txt"):
                try:
                    raw = item.read_text(encoding="utf-8", errors="replace")
                    if len(raw) <= 50000:
                        lut_content = raw
                except OSError:
                    pass

        return JSONResponse({
            "success": True,
            "sample_uuid": safe_uuid,
            "storage_label": sample_dir.parent.name,
            "meta": meta_clean,
            "images": images,
            "lut_content": lut_content,
            "file_list": file_list,
        })

    @router.get("/api/train/samples/{sample_uuid}/thumbnail")
    async def api_train_sample_thumbnail(
        sample_uuid: str,
        storage_label: str = "",
        size: int = 64,
        authorization: Optional[str] = Header(None),
    ):
        """返回 target 行内缩略图 base64，用于样本列表左侧小图。
        首次访问时解码 RAW/普通图并写入 target.thumb.jpg 磁盘缓存，后续直接读缓存文件。
        """
        if not _is_admin_request(authorization):
            raise HTTPException(status_code=403, detail="缩略图查看仅限管理员")

        safe_uuid = _safe_filename(sample_uuid)
        sample_dir = _resolve_training_sample_dir(safe_uuid, storage_label)

        target = next(
            (f for f in sample_dir.iterdir()
             if f.is_file() and f.name.lower().startswith("target")),
            None,
        )
        if not target:
            raise HTTPException(status_code=404, detail="该样本没有 target 原图")

        import cv2
        thumb_path = sample_dir / "target.thumb.jpg"
        try:
            # 磁盘缓存：如果 target.thumb.jpg 存在且比原图新，直接读文件
            if thumb_path.exists() and thumb_path.stat().st_mtime >= target.stat().st_mtime:
                arr = np.fromfile(str(thumb_path), dtype=np.uint8)
                buf = arr.tobytes()
            else:
                bgr, _ = load_image_bgr(str(target), target_size=max(size, 128), mode="preview")
                h, w = bgr.shape[:2]
                if h > size or w > size:
                    scale = size / max(h, w)
                    bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok:
                    raise ValueError("imencode failed")
                buf = buf.tobytes()
                # 写磁盘缓存（失败不影响本次返回）
                try:
                    with open(thumb_path, "wb") as f:
                        f.write(buf)
                except OSError:
                    pass
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"缩略图生成失败: {exc}")
        b64 = base64.b64encode(buf).decode("ascii")

        return JSONResponse({
            "success": True,
            "thumbnail": f"data:image/jpeg;base64,{b64}",
        })

    @router.post("/api/train/import")
    async def api_train_import(
        data: dict,
        authorization: Optional[str] = Header(None),
    ):
        """把选中的训练样本复制到 .active 目录，仅管理员可用。"""
        if not _is_admin_request(authorization):
            raise HTTPException(status_code=403, detail="训练样本导入仅限管理员")

        sample_uuids = data.get("sample_uuids", [])
        storage_labels = data.get("storage_labels", [])
        if not isinstance(sample_uuids, list) or not sample_uuids:
            raise HTTPException(status_code=400, detail="请至少选择一个样本")
        if not isinstance(storage_labels, list) or len(storage_labels) != len(sample_uuids):
            raise HTTPException(status_code=400, detail="sample_uuids 和 storage_labels 数量不一致")

        corpus_root = get_training_corpus_dir()
        active_dir = corpus_root / ".active"

        # 清空旧数据
        if active_dir.exists():
            shutil.rmtree(active_dir, ignore_errors=True)
        active_dir.mkdir(parents=True, exist_ok=True)

        imported = 0
        skipped = 0
        for su, sl in zip(sample_uuids, storage_labels):
            safe_uuid = _safe_filename(str(su))
            # storage_label 是真实目录名（可能含 @），_training_corpus_dir_for_label 内部已做校验
            sample_dir = _training_corpus_dir_for_label(str(sl)) / safe_uuid
            if not sample_dir.exists() or not sample_dir.is_dir():
                skipped += 1
                continue
            copied = 0
            for item in sample_dir.iterdir():
                if not item.is_file():
                    continue
                name_lower = item.name.lower()
                # DNCM/NeuralPreset 训练只需要原图（target），不复制 reference/result/lut/meta
                # 2026-07-15 调试：用户反馈 RAW 格式图片缩略图显示不出来，根因是导入时没把 RAW 原图复制到 .active。
                # 这里把 RAW 扩展名也加进 target 复制条件，训练统计才能数到这些图。
                _target_image_exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff") + tuple(RAW_EXTS)
                if name_lower.startswith("target") and name_lower.endswith(_target_image_exts):
                    dest = active_dir / f"{safe_uuid}_{item.name}"
                    shutil.copy2(str(item), str(dest))
                    copied += 1
            if copied > 0:
                imported += 1
            else:
                skipped += 1

        stats = get_training_data_stats_payload(str(active_dir))
        return JSONResponse({
            "success": True,
            "imported_count": imported,
            "skipped_count": skipped,
            "active_dir": str(active_dir),
            "training_file_count": stats["training_file_count"],
            "training_size_mb": stats["training_size_mb"],
        })

    return router


def _safe_filename(name: str) -> str:
    """把前端传来的文件名或 uuid 清洗成安全的目录名/文件名。"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not safe:
        return uuid.uuid4().hex
    return safe


def _resolve_training_sample_dir(sample_uuid: str, storage_label: str) -> Path:
    safe_uuid = _safe_filename(sample_uuid)
    if not storage_label:
        raise HTTPException(status_code=400, detail="storage_label 不能为空")

    try:
        exact = _training_corpus_dir_for_label(storage_label) / safe_uuid
    except ValueError:
        raise HTTPException(status_code=400, detail="storage_label 无效")

    if exact.exists() and exact.is_dir():
        return exact

    corpus_root = get_training_corpus_dir()
    matches = []
    if corpus_root.exists() and corpus_root.is_dir():
        for user_dir in corpus_root.iterdir():
            if not user_dir.is_dir() or user_dir.name.startswith("."):
                continue
            candidate = user_dir / safe_uuid
            if candidate.exists() and candidate.is_dir():
                matches.append(candidate)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="训练样本归属不唯一")
    raise HTTPException(status_code=404, detail="训练样本不存在")
