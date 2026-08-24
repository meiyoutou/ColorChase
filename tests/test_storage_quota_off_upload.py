"""存储配额默认 OFF 的真实上传测试（已登录 + 真实文件落盘）。"""
import os
from pathlib import Path


os.environ.setdefault(
    "COLORCHASE_DATABASE_URL",
    "mysql+pymysql://colorchase:password@127.0.0.1:3306/colorchase_test",
)


def _make_router(monkeypatch, tmp_path, request_user_id=1):
    import app.routes.training as rt

    storage = tmp_path / "storage"
    users_root = storage / "users"
    corpus_root = storage / "training" / "corpus"

    async def fake_resolve(uid):
        return "user_u%d" % uid

    monkeypatch.setattr(rt, "resolve_user_storage_label", fake_resolve)
    monkeypatch.setattr(
        rt, "_user_assets_root_for_label",
        lambda label: users_root / label,
    )
    monkeypatch.setattr(
        rt, "_training_corpus_dir_for_label",
        lambda label: corpus_root / label,
    )
    monkeypatch.setattr(rt, "get_training_corpus_dir", lambda: corpus_root)

    from app.routes.training import create_training_router

    router = create_training_router(
        progress_manager=object(),
        get_request_user_id=lambda auth: request_user_id,
        get_request_user_role=lambda auth: "user",
        write_task_log=lambda **kw: None,
        get_training_data_stats_payload=lambda path: {"training_file_count": 0, "training_size_mb": 0, "image_dir": path},
        resolve_training_dir=lambda path: path,
        training_image_extensions=(".jpg", ".jpeg"),
        run_training_task=lambda *a, **k: None,
        neuralpreset_model_dir="",
    )
    return router, users_root, corpus_root


def _mk_client(monkeypatch, router):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_client(monkeypatch, router):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_detection_upload_default_off_writes_file(monkeypatch, tmp_path):
    router, users_root, _corpus = _make_router(monkeypatch, tmp_path, request_user_id=7)
    client = _make_client(monkeypatch, router)

    res = client.post(
        "/api/detection/upload",
        files={"file": ("a.jpg", b"HELLO-DETECTION", "image/jpeg")},
        headers={"authorization": "Bearer t"},
    )
    assert res.status_code == 200
    det_root = users_root / "user_u7" / "detection"
    files = [p for p in det_root.rglob("*") if p.is_file()]
    assert files, "检测库应有落盘文件"
    assert b"HELLO-DETECTION" in files[0].read_bytes()


def test_training_upload_default_off_writes_files(monkeypatch, tmp_path):
    router, _u, corpus_root = _make_router(monkeypatch, tmp_path, request_user_id=3)
    client = _make_client(monkeypatch, router)

    res = client.post(
        "/api/training/upload",
        files={
            "target": ("target.jpg", b"T", "image/jpeg"),
            "result": ("result.jpg", b"R", "image/jpeg"),
        },
        headers={"authorization": "Bearer t"},
    )
    assert res.status_code == 200
    sample_root = corpus_root / "user_u3"
    targets = {p.name: p for p in sample_root.rglob("target*") if p.is_file()}
    results = {p.name: p for p in sample_root.rglob("result*") if p.is_file()}
    assert targets and results, "训练上传应落盘 target/result"
    assert list(targets.values())[0].read_bytes() == b"T"
    assert list(results.values())[0].read_bytes() == b"R"