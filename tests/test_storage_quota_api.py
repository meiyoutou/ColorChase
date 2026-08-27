"""用户配额自查接口的真实响应测试。"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import training as rt
from models import UserStorageQuota


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return _Scalars(self.values)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Result(self.rows)


async def _no_db():
    yield _Session([])


def _client(rows):
    router = rt.create_training_router(
        progress_manager=object(),
        get_request_user_id=lambda _auth: 7,
        get_request_user_role=lambda _auth: "user",
        write_task_log=lambda **_kwargs: None,
        get_training_data_stats_payload=lambda path: {
            "training_file_count": 0, "training_size_mb": 0, "image_dir": path,
        },
        resolve_training_dir=lambda path: path,
        training_image_extensions=(".jpg",),
        run_training_task=lambda *_args, **_kwargs: None,
        neuralpreset_model_dir="",
    )
    app = FastAPI()
    app.include_router(router)

    async def db_override():
        yield _Session(rows)

    app.dependency_overrides[rt.get_db] = db_override
    return TestClient(app)


def test_quota_endpoint_returns_both_default_kinds_without_rows():
    response = _client([]).get(
        "/api/storage/quota", headers={"authorization": "Bearer test"}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["quota"]) == {"training", "detection"}
    for details in body["quota"].values():
        assert details["used_bytes"] == 0
        assert details["reserved_bytes"] == 0
        assert details["quota_bytes"] > 0
        assert details["remaining_bytes"] == details["quota_bytes"]
        assert details["reconcile_needed"] is False
        assert details["reconcile_reason"] is None


def test_quota_endpoint_exposes_reconcile_state():
    marked = datetime(2026, 8, 27, 12, 0, 0)
    row = UserStorageQuota(
        user_id=7, kind="training", used_bytes=100, reserved_bytes=20,
        quota_mb=1, reconcile_needed=True, reconcile_reason="reservation_expired",
        reconcile_sample_key="sample-x", reconcile_marked_at=marked,
    )
    response = _client([row]).get(
        "/api/storage/quota", headers={"authorization": "Bearer test"}
    )

    assert response.status_code == 200
    details = response.json()["quota"]["training"]
    assert details["reconcile_needed"] is True
    assert details["reconcile_reason"] == "reservation_expired"
    assert details["reconcile_sample_key"] == "sample-x"
    assert details["reconcile_marked_at"] == str(marked)
