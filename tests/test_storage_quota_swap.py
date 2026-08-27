"""配额启用路径的目录交换与组合锁真实路由测试（不依赖 MySQL）。"""
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import training as rt
from app.services import storage_quota as sq


def _router(monkeypatch, tmp_path):
    users_root = tmp_path / "users"
    corpus_root = tmp_path / "training" / "corpus"

    async def label(_uid):
        return "user_u1"

    monkeypatch.setattr(rt, "resolve_user_storage_label", label)
    monkeypatch.setattr(rt, "_user_assets_root_for_label", lambda value: users_root / value)
    monkeypatch.setattr(rt, "_training_corpus_dir_for_label", lambda value: corpus_root / value)
    monkeypatch.setattr(rt, "get_training_corpus_dir", lambda: corpus_root)

    router = rt.create_training_router(
        progress_manager=object(),
        get_request_user_id=lambda _auth: 1,
        get_request_user_role=lambda _auth: "user",
        write_task_log=lambda **_kwargs: None,
        get_training_data_stats_payload=lambda path: {
            "training_file_count": 0, "training_size_mb": 0, "image_dir": path,
        },
        resolve_training_dir=lambda path: path,
        training_image_extensions=(".jpg", ".jpeg", ".png"),
        run_training_task=lambda *_args, **_kwargs: None,
        neuralpreset_model_dir="",
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), users_root, corpus_root


def _install_enabled_quota(monkeypatch, *, reserve_allowed=True):
    events = []

    @asynccontextmanager
    async def operation_lock(user_id, kind, sample_key, timeout=5.0):
        events.append(("lock", user_id, kind, sample_key))
        yield sq.QuotaOperationLock(
            type("T", (), {"acquired": True})(),
            type("T", (), {"acquired": True})(),
        )

    async def reserve(*_args, **kwargs):
        events.append(("reserve", kwargs.get("lock_context")))
        if reserve_allowed:
            return sq.ReserveOutcome(allowed=True, reservation_id="r1")
        return sq.ReserveOutcome(allowed=False, reason="quota_exceeded")

    async def touch(*_args, **_kwargs):
        events.append(("touch",))
        return True

    async def mark(*_args, **kwargs):
        events.append(("mark", kwargs.get("reason"), kwargs.get("lock_context")))

    async def settle(*_args, **kwargs):
        events.append(("settle", kwargs.get("lock_context")))
        return 0

    async def record(*_args, **kwargs):
        events.append(("record", kwargs.get("lock_context")))
        return 0

    async def release(*_args, **kwargs):
        events.append(("release", kwargs.get("stored"), kwargs.get("lock_context")))

    async def reconcile(*_args, **kwargs):
        events.append(("reconcile", kwargs.get("lock_context")))
        return 0

    monkeypatch.setattr(rt.storage_quota_svc, "is_quota_enabled", lambda: True)
    monkeypatch.setattr(rt.storage_quota_svc, "is_dry_run", lambda: False)
    monkeypatch.setattr(rt.storage_quota_svc, "quota_operation_lock", operation_lock)
    monkeypatch.setattr(rt.storage_quota_svc, "reserve_quota", reserve)
    monkeypatch.setattr(rt.storage_quota_svc, "touch_reservation", touch)
    monkeypatch.setattr(rt.storage_quota_svc, "mark_reconcile_needed", mark)
    monkeypatch.setattr(rt.storage_quota_svc, "settle_quota", settle)
    monkeypatch.setattr(rt.storage_quota_svc, "release_reservation", release)
    monkeypatch.setattr(rt.storage_quota_svc, "record_usage", record)
    monkeypatch.setattr(rt.storage_quota_svc, "reconcile_usage", reconcile)
    return events


def _post_training(client, *, sample="sample1", target=b"NEW-T", result=b"NEW-R"):
    return client.post(
        "/api/training/upload",
        files={
            "target": ("target.png", target, "image/png"),
            "result": ("result.png", result, "image/png"),
        },
        data={"sample_uuid": sample},
        headers={"authorization": "Bearer test"},
    )


def test_detection_route_uses_combination_lock_and_swaps_directory(monkeypatch, tmp_path):
    client, users, _corpus = _router(monkeypatch, tmp_path)
    events = _install_enabled_quota(monkeypatch)
    target = users / "user_u1" / "detection" / "det1"
    target.mkdir(parents=True)
    (target / "original.jpg").write_bytes(b"OLD")

    response = client.post(
        "/api/detection/upload",
        files={"file": ("new.png", b"NEW-DETECTION", "image/png")},
        data={"file_uuid": "det1"},
        headers={"authorization": "Bearer test"},
    )

    assert response.status_code == 200
    assert (target / "original.png").read_bytes() == b"NEW-DETECTION"
    assert not (target / "original.jpg").exists()
    assert ("lock", 1, "detection", "det1") in events
    assert any(event[0] == "mark" for event in events)
    assert any(event[0] == "settle" for event in events)


def test_quota_rejection_leaves_no_sample_or_staging(monkeypatch, tmp_path):
    client, _users, corpus = _router(monkeypatch, tmp_path)
    _install_enabled_quota(monkeypatch, reserve_allowed=False)

    response = _post_training(client)

    assert response.status_code == 413
    user_root = corpus / "user_u1"
    assert not (user_root / "sample1").exists()
    assert not list(user_root.glob("sample1.staging-*")) if user_root.exists() else True


def test_successful_swap_preserves_optional_files_and_updates_meta(monkeypatch, tmp_path):
    client, _users, corpus = _router(monkeypatch, tmp_path)
    events = _install_enabled_quota(monkeypatch)
    sample = corpus / "user_u1" / "sample1"
    sample.mkdir(parents=True)
    (sample / "target.jpg").write_bytes(b"OLD-T")
    (sample / "result.jpg").write_bytes(b"OLD-R")
    (sample / "reference.jpg").write_bytes(b"KEEP-REF")
    (sample / "lut.cube").write_bytes(b"KEEP-LUT")
    (sample / "meta.json").write_text("{}", encoding="utf-8")

    response = _post_training(client)

    assert response.status_code == 200
    assert (sample / "target.png").read_bytes() == b"NEW-T"
    assert (sample / "result.png").read_bytes() == b"NEW-R"
    assert not (sample / "target.jpg").exists()
    assert not (sample / "result.jpg").exists()
    assert (sample / "reference.jpg").read_bytes() == b"KEEP-REF"
    assert (sample / "lut.cube").read_bytes() == b"KEEP-LUT"
    meta = json.loads((sample / "meta.json").read_text(encoding="utf-8"))
    assert meta["saved_files"] == {
        "target": "target.png", "result": "result.png",
        "reference": "reference.jpg", "lut": "lut.cube",
    }
    assert any(event[0] == "lock" for event in events)
    assert any(event[0] == "mark" for event in events)
    assert any(event[0] == "settle" for event in events)
    assert not list(sample.parent.glob("sample1.staging-*"))
    assert not list(sample.parent.glob("sample1.backup-committed-*"))


def test_failed_swap_and_failed_restore_leave_recovery_backup(monkeypatch, tmp_path):
    client, _users, corpus = _router(monkeypatch, tmp_path)
    _install_enabled_quota(monkeypatch)
    sample = corpus / "user_u1" / "sample1"
    sample.mkdir(parents=True)
    (sample / "target.jpg").write_bytes(b"OLD-T")
    (sample / "result.jpg").write_bytes(b"OLD-R")
    (sample / "meta.json").write_text("{}", encoding="utf-8")
    real_rename = rt.os.rename
    calls = {"count": 0}

    def fail_after_backup(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:
            return real_rename(src, dst)
        raise OSError("rename failed")

    monkeypatch.setattr(rt.os, "rename", fail_after_backup)
    try:
        _post_training(client)
    except OSError:
        pass

    assert not sample.exists()
    backups = list(sample.parent.glob("sample1.backup-recovery-*"))
    assert len(backups) == 1
    assert (backups[0] / "target.jpg").read_bytes() == b"OLD-T"
    assert (backups[0] / "result.jpg").read_bytes() == b"OLD-R"


def test_optional_copy_failure_keeps_old_sample_unchanged(monkeypatch, tmp_path):
    client, _users, corpus = _router(monkeypatch, tmp_path)
    _install_enabled_quota(monkeypatch)
    sample = corpus / "user_u1" / "sample1"
    sample.mkdir(parents=True)
    original = {
        "target.jpg": b"OLD-T", "result.jpg": b"OLD-R",
        "reference.jpg": b"KEEP-REF", "meta.json": b"{}",
    }
    for name, content in original.items():
        (sample / name).write_bytes(content)

    real_copy = rt.shutil.copy2

    def fail_reference(src, dst, *args, **kwargs):
        if str(src).endswith("reference.jpg"):
            raise OSError("copy failed")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(rt.shutil, "copy2", fail_reference)
    try:
        _post_training(client)
    except OSError:
        pass

    assert {p.name: p.read_bytes() for p in sample.iterdir() if p.is_file()} == original
    assert not list(sample.parent.glob("sample1.staging-*"))
    assert not list(sample.parent.glob("sample1.backup-*"))
