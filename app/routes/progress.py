import asyncio
import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse


TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def create_progress_router(progress_manager):
    router = APIRouter()

    @router.get("/api/progress/{task_id}")
    async def api_progress(task_id: str):
        if not TASK_ID_RE.fullmatch(str(task_id or "")):
            raise HTTPException(status_code=400, detail="Invalid task id")

        queue = progress_manager.get_queue(task_id)
        if queue is None:
            # 前端会先打开进度通道，再发起真正的追色请求。这里先占位，避免启动阶段误报 404。
            progress_manager.register_task(task_id)
            queue = progress_manager.get_queue(task_id)
        if queue is None:
            raise HTTPException(status_code=404, detail="Task not found")

        async def event_generator():
            try:
                latest = progress_manager.get_progress(task_id)
                if latest:
                    yield f"data: {json.dumps(latest, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'stage': 'waiting', 'progress': 2, 'message': '等待任务启动...'}, ensure_ascii=False)}\n\n"

                while True:
                    if progress_manager.is_cancelled(task_id):
                        yield f"data: {json.dumps({'stage': 'cancelled', 'progress': 0, 'message': '任务已取消'}, ensure_ascii=False)}\n\n"
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=30)
                        yield f"data: {data}\n\n"
                        parsed = json.loads(data)
                        if parsed.get("stage") in ("done", "error", "cancelled"):
                            break
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'stage': 'heartbeat', 'progress': 0, 'message': ''}, ensure_ascii=False)}\n\n"
            finally:
                progress_manager.remove_task(task_id)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router
