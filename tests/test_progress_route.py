import asyncio
import json

from app.routes.progress import create_progress_router


class FakeProgressManager:
    def __init__(self):
        self.queues = {}
        self.latest = {}
        self.removed = []

    def get_queue(self, task_id):
        return self.queues.get(task_id)

    def register_task(self, task_id):
        self.queues.setdefault(task_id, asyncio.Queue())
        return task_id

    def get_progress(self, task_id):
        return self.latest.get(task_id)

    def is_cancelled(self, task_id):
        return False

    def remove_task(self, task_id):
        self.removed.append(task_id)
        self.queues.pop(task_id, None)


def test_progress_route_registers_unknown_task_before_transfer_starts():
    async def run():
        manager = FakeProgressManager()
        router = create_progress_router(manager)
        endpoint = router.routes[0].endpoint

        response = await endpoint("task_123")
        first_event = await anext(response.body_iterator)
        await response.body_iterator.aclose()

        payload = first_event.removeprefix("data: ").strip()
        data = json.loads(payload)
        assert data["stage"] == "waiting"
        assert "task_123" in manager.removed

    asyncio.run(run())
