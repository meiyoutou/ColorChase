import os
import subprocess
import tempfile
import uuid
from typing import Optional

import cv2
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from admin_runtime_metrics import record_export, record_task_log, record_user_usage
from algorithms.video.processor import _get_ffmpeg_path
from app.routes.projects import _user_profile_record
from app.security import ensure_upload_file_size
from app.services.auth_utils import _get_request_user_id, _get_request_user_role
from app.services.paths import (
    _ensure_project_access,
    _resolve_local_file_path,
    _runtime_video_dir,
    _safe_project_bucket_dir,
)
from app.services.task_logging import create_task_log_writer
from config import BASE_DIR

router = APIRouter()
_write_task_log = create_task_log_writer(BASE_DIR, _user_profile_record, record_task_log)


@router.post("/api/export_video")
async def api_export_video(
    source_url: str = Form(...),
    format: str = Form("mp4"),
    bitrate: str = Form("8"),
    resolution: str = Form("original"),
    fps: str = Form("original"),
    project_id: int = Form(0),
    authorization: Optional[str] = Header(None),
):
    request_user_id = _get_request_user_id(authorization)
    request_user_role = _get_request_user_role(authorization)
    project_id = await _ensure_project_access(project_id, request_user_id)
    source_resolved = _resolve_local_file_path(source_url)
    source_path = str(source_resolved) if source_resolved else os.path.join(str(_runtime_video_dir()), os.path.basename(source_url))
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="源视频文件不存在")

    ffmpeg = _get_ffmpeg_path()

    ext_map = {"mp4": ".mp4", "mp4_h265": ".mp4", "mov": ".mov", "avi": ".avi"}
    export_filename = f"export_{uuid.uuid4().hex}{ext_map.get(format, '.mp4')}"
    if project_id > 0:
        export_path = str(_safe_project_bucket_dir(project_id, "video_exports") / export_filename)
    else:
        export_path = os.path.join(str(_runtime_video_dir()), export_filename)

    cmd = [ffmpeg, "-y", "-i", source_path]

    codec_map = {
        "mp4": "libx264",
        "mp4_h265": "libx265",
        "mov": "prores_ks",
        "avi": "rawvideo",
    }
    vcodec = codec_map.get(format, "libx264")

    cmd += ["-c:v", vcodec]

    if format in ("mp4", "mp4_h265"):
        cmd += ["-b:v", f"{bitrate}M"]

    if resolution != "original":
        res_map = {"4k": "3840:2160", "1080p": "1920:1080", "720p": "1280:720", "480p": "854:480"}
        if resolution in res_map:
            cmd += ["-vf", f"scale={res_map[resolution]}"]

    if fps != "original":
        cmd += ["-r", str(fps)]

    if format == "avi":
        cmd += ["-pix_fmt", "bgr24"]
    elif format == "mov":
        cmd += ["-pix_fmt", "yuv422p10le", "-profile:v", "3"]
    else:
        cmd += ["-pix_fmt", "yuv420p"]

    if format == "mov":
        cmd += ["-c:a", "pcm_s16le"]
    elif format == "avi":
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd.append(export_path)

    try:
        subprocess.run(cmd, check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        if os.path.exists(export_path):
            os.remove(export_path)
        raise HTTPException(status_code=500, detail=f"导出转码失败: {str(e)}")

    record_export(1)
    export_size_bytes = os.path.getsize(export_path) if os.path.exists(export_path) else 0
    if request_user_id is not None:
        record_user_usage(request_user_id)
    _write_task_log(
        task_id="",
        task_type="导出",
        event_type="result",
        status="ok",
        summary="视频导出完成",
        detail=f"格式: {format}",
        user_id=request_user_id,
        role=request_user_role,
        model=format,
        meta={
            "bitrate": bitrate,
            "resolution": resolution,
            "fps": fps,
            "export_format": format,
            "export_size_bytes": export_size_bytes,
            "export_path": export_path,
            "project_id": project_id,
        },
    )
    return FileResponse(
        export_path,
        media_type="video/mp4" if format in ("mp4", "mp4_h265") else f"video/{format}",
        filename=f"ColorChase_export.{'mov' if format == 'mov' else 'avi' if format == 'avi' else 'mp4'}",
    )


@router.post("/api/video_metadata")
async def api_video_metadata(video: UploadFile = File(...)):
    ensure_upload_file_size(video, 300 * 1024 * 1024, label="视频文件")
    tmp_path = None
    cap = None
    try:
        tmp_dir = tempfile.gettempdir()
        tmp_name = uuid.uuid4().hex + os.path.splitext(video.filename or "video.mp4")[1]
        tmp_path = os.path.join(tmp_dir, tmp_name)
        contents = await video.read()
        with open(tmp_path, "wb") as f:
            f.write(contents)
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            raise ValueError("无法打开视频文件")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 25.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = frame_count / fps if fps > 0 else 0
        codec = "h264"
        try:
            fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
            codec = "".join([chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)]).strip()
        except Exception:
            pass
        return JSONResponse({
            "fps": round(fps, 2),
            "duration": round(duration, 2),
            "width": width,
            "height": height,
            "codec": codec,
            "frame_count": frame_count,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if cap is not None:
            cap.release()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
