"""存储配额默认 OFF 的路由集成测试。

验证：默认（QUOTA_ENABLED=0）时 /api/detection/upload 与 /api/training/upload
用 TestClient 能正常响应（不进包含配额分支，不 500，行为与旧版一致），
以及 storage_quota.is_quota_enabled()/is_dry_run() 默认值。
"""
import os


os.environ.setdefault(
    "COLORCHASE_DATABASE_URL",
    "mysql+pymysql://colorchase:password@127.0.0.1:3306/colorchase_test",
)


def test_quota_switch_default_off():
    from app.services import storage_quota as sq

    assert sq.is_quota_enabled() is False
    assert sq.is_dry_run() is True


def test_detection_upload_default_off_route_starts(monkeypatch, tmp_path):
    # 默认 OFF：路由应为 401（未登录）而非 500（进入配额分支抛 AttributeError）
    from fastapi.testclient import TestClient
    from app.routes.training import create_training_router

    router = create_training_router(
        progress_manager=object(),
        get_request_user_id=lambda auth: None,  # 未登录
        get_request_user_role=lambda auth: "",
        write_task_log=lambda **kw: None,
        get_training_data_stats_payload=lambda path: {"training_file_count": 0, "training_size_mb": 0, "image_dir": path},
        resolve_training_dir=lambda path: None,
        training_image_extensions=(".jpg", ".jpeg"),
        run_training_task=lambda *a, **k: None,
        neuralpreset_model_dir="",
    )
    # 挂到一个最小 app
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # 未登录：应返回 401（get_request_user_id -> None）
    res = client.post("/api/detection/upload", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert res.status_code in (401, 200, 422)


def test_training_upload_default_off_starts(monkeypatch):
    from fastapi.testclient import TestClient
    from app.routes.training import create_training_router
    from fastapi import FastAPI

    router = create_training_router(
        progress_manager=object(),
        get_request_user_id=lambda auth: None,
        get_request_user_role=lambda auth: "",
        write_task_log=lambda **kw: None,
        get_training_data_stats_payload=lambda **kw: {"training_file_count": 0, "training_size_mb": 0, "image_dir": ""},
        resolve_training_dir=lambda path: None,
        training_image_extensions=(".jpg",),
        run_training_task=lambda *a, **k: None,
        neuralpreset_model_dir="",
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    res = client.post(
        "/api/training/upload",
        files={"target": ("t.jpg", b"x", "image/jpeg"), "result": ("r.jpg", b"x", "image/jpeg")},
    )
    assert res.status_code in (401, 200, 422)