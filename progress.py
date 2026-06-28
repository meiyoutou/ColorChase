import asyncio
import json
import uuid
from typing import Dict


class ProgressManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._streams: Dict[str, asyncio.Queue] = {}
            cls._instance._task_status: Dict[str, Dict] = {}
            cls._instance._latest_progress: Dict[str, Dict] = {}
        return cls._instance

    def create_task(self) -> str:
        task_id = uuid.uuid4().hex[:12]
        self._streams[task_id] = asyncio.Queue()
        self._task_status[task_id] = {"paused": False, "cancelled": False}
        return task_id

    def register_task(self, task_id: str):
        if task_id not in self._streams:
            self._streams[task_id] = asyncio.Queue()
        if task_id not in self._task_status:
            self._task_status[task_id] = {"paused": False, "cancelled": False}
        return task_id

    async def send(self, task_id: str, stage: str, progress: float, message: str = "", **extra):
        if task_id in self._streams:
            data = {"stage": stage, "progress": round(progress, 2), "message": message, **extra}
            self._latest_progress[task_id] = data
            await self._streams[task_id].put(json.dumps(data, ensure_ascii=False))

    def get_progress(self, task_id: str):
        return self._latest_progress.get(task_id)

    def get_queue(self, task_id: str) -> asyncio.Queue:
        return self._streams.get(task_id)

    def remove_task(self, task_id: str):
        self._streams.pop(task_id, None)
        self._task_status.pop(task_id, None)
        self._latest_progress.pop(task_id, None)

    def pause_task(self, task_id: str) -> bool:
        if task_id in self._task_status:
            self._task_status[task_id]["paused"] = True
            return True
        return False

    def resume_task(self, task_id: str) -> bool:
        if task_id in self._task_status:
            self._task_status[task_id]["paused"] = False
            return True
        return False

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self._task_status:
            self._task_status[task_id]["cancelled"] = True
            self._task_status[task_id]["paused"] = False
            return True
        return False

    def is_paused(self, task_id: str) -> bool:
        return self._task_status.get(task_id, {}).get("paused", False)

    def is_cancelled(self, task_id: str) -> bool:
        return self._task_status.get(task_id, {}).get("cancelled", False)

    def active_task_count(self) -> int:
        active_count = 0
        for task_id in list(self._task_status.keys()):
            latest = self._latest_progress.get(task_id, {})
            stage = latest.get("stage")
            if stage in {"done", "error", "cancelled"}:
                continue
            if self._task_status.get(task_id, {}).get("cancelled", False):
                continue
            active_count += 1
        return active_count


progress_manager = ProgressManager()
