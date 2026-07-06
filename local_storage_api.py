"""本地存储 API - 检测库和训练库上传接口"""
import os
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException, Depends, Request
from app.services.auth_utils import _get_request_user_id, _get_request_user_role
from app.services.paths import (
    _is_admin_request,
    _runtime_user_temp_url,
    _save_to_runtime_user_temp,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select

from database import async_session
from models import User

router = APIRouter()

STORAGE_DIR = Path(__file__).resolve().parent / "storage"

# 存储根目录
STORAGE_DIR = Path(__file__).resolve().parent / "storage"
DETECTION_DIR = STORAGE_DIR / "detection"
TRAINING_CORPUS_DIR = STORAGE_DIR / "training" / "corpus"


def _sanitize_email(email: str) -> str:
    """邮箱转安全目录名：@ 和 . 替换成 _"""
    return email.replace("@", "_").replace(".", "_") if email else "unknown"


async def _require_local_storage(request: Request):
    """强制要求客户端具备本地存储能力（fsa 或 agent 模式）。
    管理员账号直接放行，不需要本地存储。
    前端在 fsa/agent 模式下会通过 fetch 拦截器自动注入 X-ColorChase-Storage-Mode 头。
    none 模式不发此头，请求被拒绝（管理员除外）。
    """
    # 管理员放行：从 JWT token 解码 role，admin 直接 return
    authorization = request.headers.get("authorization")
    if authorization:
        try:
            role = _get_request_user_role(authorization)
            if role == "admin":
                return
        except Exception:
            pass
    # 非管理员校验存储模式
    storage_mode = request.headers.get("X-ColorChase-Storage-Mode", "")
    if storage_mode not in ("fsa", "agent"):
        raise HTTPException(
            status_code=400,
            detail="请先启动 ColorChaseAgent 或使用 Chrome/Edge 浏览器（需要 File System Access API）。Firefox 用户请下载并运行 ColorChaseAgent。"
        )

async def _get_user_email(authorization: Optional[str]) -> str:
    """从 token 获取用户邮箱"""
    user_id = _get_request_user_id(authorization)
    if user_id is None:
        return "unknown"
    try:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                return user.email or user.phone or f"user_{user_id}"
    except Exception:
        pass
    return "unknown"


@router.post("/api/detection/upload")
async def api_detection_upload(
    file: UploadFile = File(...),
    file_uuid: str = Form(""),
    authorization: Optional[str] = Header(None),
):
    """上传原图/原视频到检测库，用户隔离。普通用户走临时目录，不落盘到检测库。"""
    from app.services.auth_utils import _get_request_user_id
    request_user_id = _get_request_user_id(authorization)
    is_admin = _is_admin_request(authorization)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    fid = file_uuid or uuid.uuid4().hex

    if is_admin:
        email = await _get_user_email(authorization)
        user_folder = _sanitize_email(email)
        save_dir = DETECTION_DIR / user_folder / fid
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f"original{ext}"
        with open(save_path, "wb") as f:
            f.write(content)
        return JSONResponse({"ok": True, "path": str(save_path), "uuid": fid})

    # 普通用户：只写入临时目录，返回临时 URL，后续由清理任务回收
    save_name = f"{fid}{ext}"
    temp_path = _save_to_runtime_user_temp(content, request_user_id, save_name)
    return JSONResponse({
        "ok": True,
        "path": str(temp_path),
        "uuid": fid,
        "temp_url": _runtime_user_temp_url(request_user_id, save_name),
    })


@router.post("/api/training/upload")
async def api_training_upload(
    storage_check=Depends(_require_local_storage),
    target: UploadFile = File(...),
    reference: UploadFile = File(None),
    result: UploadFile = File(...),
    meta: str = Form("{}"),
    sample_uuid: str = Form(""),
    is_video: str = Form("0"),
    authorization: Optional[str] = Header(None),
):
    """导出时上传完整文件组到训练库，按评分+用户+时间隔离。普通用户数据不落盘到服务器训练库。
    视频不进训练库
    """
    # 视频不上传训练库
    if is_video == "1":
        return JSONResponse({"ok": True, "skipped": "video"})

    # 解析 meta
    try:
        meta_data = json.loads(meta)
    except Exception:
        meta_data = {}

    rating = int(meta_data.get("rating", 0))
    if rating == 0:
        return JSONResponse({"ok": True, "skipped": "no_rating"})

    # 普通用户：评分记录只在本地保存，不上传到服务器训练库
    if not _is_admin_request(authorization):
        return JSONResponse({"ok": True, "skipped": "non_admin_no_server_storage"})

    email = await _get_user_email(authorization)
    user_folder = _sanitize_email(email)

    # 按评分分档
    if rating >= 3:
        tier = "high_rating"
    else:
        tier = "low_rating"

    # 导出时间分组
    export_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    sample_id = sample_uuid or uuid.uuid4().hex

    sample_dir = TRAINING_CORPUS_DIR / tier / user_folder / export_time / sample_id
    sample_dir.mkdir(parents=True, exist_ok=True)

    # 保存 target
    target_ext = os.path.splitext(target.filename)[1] if target.filename else ".jpg"
    target_content = await target.read()
    with open(sample_dir / f"target{target_ext}", "wb") as f:
        f.write(target_content)

    # 保存 reference
    if reference and reference.filename:
        ref_ext = os.path.splitext(reference.filename)[1] if reference.filename else ".jpg"
        ref_content = await reference.read()
        with open(sample_dir / f"reference{ref_ext}", "wb") as f:
            f.write(ref_content)

    # 保存 result
    result_ext = os.path.splitext(result.filename)[1] if result.filename else ".jpg"
    result_content = await result.read()
    with open(sample_dir / f"result{result_ext}", "wb") as f:
        f.write(result_content)

    # 保存 meta
    meta_data["archived_at"] = datetime.now().isoformat()
    meta_data["user_email"] = email
    meta_data["rating"] = rating
    with open(sample_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta_data, f, ensure_ascii=False, indent=2)

    return JSONResponse({
        "ok": True,
        "tier": tier,
        "path": str(sample_dir),
        "sample_id": sample_id,
    })


@router.get("/api/detection/stats")
async def api_detection_stats(authorization: Optional[str] = Header(None)):
    """检测库统计"""
    email = await _get_user_email(authorization)
    user_folder = _sanitize_email(email)
    user_dir = DETECTION_DIR / user_folder
    count = 0
    size_mb = 0
    if user_dir.exists():
        for f in user_dir.rglob("*"):
            if f.is_file():
                count += 1
                size_mb += f.stat().st_size
    return JSONResponse({
        "count": count,
        "size_mb": round(size_mb / 1024 / 1024, 2),
    })
