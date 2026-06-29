import asyncio
from json import JSONDecodeError
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from progress import progress_manager
from app.security import require_local_admin_tools_enabled
from config import DEFAULT_PATHS, ensure_runtime_dirs, get_current_runtime_path_strings


def create_task_router(get_request_user_id, get_request_user_role, write_task_log):
    router = APIRouter()

    @router.post('/api/task/{task_id}/pause')
    async def api_pause_task(task_id: str, authorization: Optional[str] = Header(None)):
        success = progress_manager.pause_task(task_id)
        write_task_log(
            task_id=task_id,
            task_type='任务控制',
            event_type='control',
            status='info' if success else 'fail',
            summary='训练任务已暂停' if success else '暂停失败，任务不存在',
            detail=task_id,
            user_id=get_request_user_id(authorization),
            role=get_request_user_role(authorization),
            model='task_control',
        )
        return JSONResponse({'success': success, 'message': '任务已暂停' if success else '任务不存在'})

    @router.post('/api/task/{task_id}/resume')
    async def api_resume_task(task_id: str, authorization: Optional[str] = Header(None)):
        success = progress_manager.resume_task(task_id)
        write_task_log(
            task_id=task_id,
            task_type='任务控制',
            event_type='control',
            status='info' if success else 'fail',
            summary='训练任务已恢复' if success else '恢复失败，任务不存在',
            detail=task_id,
            user_id=get_request_user_id(authorization),
            role=get_request_user_role(authorization),
            model='task_control',
        )
        return JSONResponse({'success': success, 'message': '任务已恢复' if success else '任务不存在'})

    @router.post('/api/task/{task_id}/cancel')
    async def api_cancel_task(task_id: str, authorization: Optional[str] = Header(None)):
        success = progress_manager.cancel_task(task_id)
        write_task_log(
            task_id=task_id,
            task_type='任务控制',
            event_type='control',
            status='cancel' if success else 'fail',
            summary='训练任务已取消' if success else '取消失败，任务不存在',
            detail=task_id,
            user_id=get_request_user_id(authorization),
            role=get_request_user_role(authorization),
            model='task_control',
        )
        return JSONResponse({'success': success, 'message': '任务已取消' if success else '任务不存在'})

    @router.get('/api/task/{task_id}/progress')
    async def api_task_progress(task_id: str):
        data = progress_manager.get_progress(task_id)
        if data is None:
            if progress_manager.get_queue(task_id) is None:
                raise HTTPException(status_code=404, detail='任务不存在')
            return JSONResponse({'current': 0, 'total': 0, 'status': 'pending', 'eta': '计算中...'})
        result = {
            'current': int(data.get('progress', 0)),
            'total': 100,
            'status': data.get('stage', 'processing'),
            'message': data.get('message', ''),
            'elapsed': data.get('elapsed', 0),
            'eta': data.get('eta', '计算中...'),
        }
        if data.get('result_url'):
            result['result_url'] = data['result_url']
        if data.get('video_fps'):
            result['video_fps'] = data['video_fps']
            result['video_width'] = data.get('video_width', 0)
            result['video_height'] = data.get('video_height', 0)
        if data.get('avg_diff') is not None:
            result['avg_diff'] = data['avg_diff']
        return JSONResponse(result)

    @router.get('/api/user_config')
    async def api_get_user_config():
        import shutil
        require_local_admin_tools_enabled()
        ensure_runtime_dirs()
        current_paths = get_current_runtime_path_strings()
        disk = shutil.disk_usage(current_paths['image_uploads'])
        return JSONResponse({
            'config': current_paths,
            'current': current_paths,
            'disk_free_gb': round(disk.free / (1024**3), 1),
        })

    @router.post('/api/user_config')
    async def api_save_user_config(request: Request):
        from config import save_user_config
        require_local_admin_tools_enabled()
        try:
            data = await request.json()
        except JSONDecodeError:
            raise HTTPException(status_code=400, detail='Request body must be valid JSON')
        valid_keys = set(DEFAULT_PATHS.keys())
        filtered = {k: v for k, v in data.items() if k in valid_keys and v and isinstance(v, str)}
        save_user_config(filtered)
        ensure_runtime_dirs()
        return JSONResponse({'success': True, 'message': '配置已保存并立即生效'})

    @router.post('/api/pick_folder')
    async def api_pick_folder():
        require_local_admin_tools_enabled()

        def _pick():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                folder = filedialog.askdirectory(title='选择存储目录')
                root.destroy()
                return folder.replace('/', '\\') if folder else ''
            except Exception:
                return ''

        folder = await asyncio.to_thread(_pick)
        return JSONResponse({'path': folder})

    return router
