import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse


def create_progress_router(progress_manager):
    router = APIRouter()

    @router.get("/api/progress/{task_id}")
    async def api_progress(task_id: str):
        queue = progress_manager.get_queue(task_id)
        if queue is None:
            raise HTTPException(status_code=404, detail="Task not found")

        async def event_generator():
            try:
                while True:
                    if progress_manager.is_cancelled(task_id):
                        yield f"data: {json.dumps({'stage': 'cancelled', 'progress': 0, 'message': '任务已取消'})}\n\n"
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=30)
                        yield f"data: {data}\n\n"
                        parsed = json.loads(data)
                        if parsed.get("stage") in ("done", "error", "cancelled"):
                            break
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'stage': 'heartbeat', 'progress': 0, 'message': ''})}\n\n"
            finally:
                progress_manager.remove_task(task_id)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router
