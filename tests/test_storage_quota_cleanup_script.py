"""过期清理脚本的安全边界测试（真实临时文件系统，无 MySQL）。"""
import asyncio
import os
import time
from contextlib import asynccontextmanager

from app.services import storage_quota as sq
from scripts import cleanup_expired_training_data as cleanup


def _old(path):
    timestamp = time.time() - 10 * 3600
    os.utime(path, (timestamp, timestamp))


def test_cleanup_main_default_off_never_opens_database(monkeypatch, capsys):
    import app.settings as settings

    monkeypatch.setattr(settings, "STORAGE_QUOTA_ENABLED", False)

    class ForbiddenSession:
        def __call__(self):
            raise AssertionError("database must not be opened while quota is disabled")

    monkeypatch.setattr(cleanup, "async_session", ForbiddenSession())
    asyncio.run(cleanup.main())
    assert "disabled" in capsys.readouterr().out.lower()


def test_residual_cleanup_never_deletes_recovery_backup(tmp_path):
    committed = tmp_path / "sample.backup-committed-deadbeef"
    recovery = tmp_path / "sample.backup-recovery-deadbeef"
    staging = tmp_path / "sample.staging-deadbeef"
    for path in (committed, recovery, staging):
        path.mkdir()
        (path / "data").write_bytes(b"x")
        _old(path)
    stats = {"deleted": 0, "failed": 0, "would_delete": 0}

    cleanup._cleanup_residuals(tmp_path, False, time.time(), stats)

    assert not committed.exists()
    assert not staging.exists()
    assert recovery.exists()
    assert stats["deleted"] == 2


def test_residual_cleanup_handles_trash_children_not_parent(tmp_path):
    trash = tmp_path / cleanup.TRASH_DIR_NAME
    child = trash / "sample-deadbeef"
    child.mkdir(parents=True)
    (child / "data").write_bytes(b"x")
    _old(child)
    stats = {"deleted": 0, "failed": 0, "would_delete": 0}

    cleanup._cleanup_residuals(tmp_path, False, time.time(), stats)

    assert trash.exists()
    assert not child.exists()
    assert stats["deleted"] == 1


def test_cleanup_kind_uses_combination_lock_and_current_dir_validation(monkeypatch, tmp_path):
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "meta.json").write_text("{}", encoding="utf-8")
    (sample / "target.jpg").write_bytes(b"t")
    (sample / "result.jpg").write_bytes(b"r")
    for path in sample.iterdir():
        old = time.time() - 40 * 86400
        os.utime(path, (old, old))
    events = []

    @asynccontextmanager
    async def op_lock(uid, kind, sample_key, timeout=2.0):
        events.append(("lock", uid, kind, sample_key))
        yield sq.QuotaOperationLock(
            type("T", (), {"acquired": True})(),
            type("T", (), {"acquired": True})(),
        )

    async def no_active(*_args, **_kwargs):
        return False

    async def fake_trash(*_args, **kwargs):
        events.append(("trash", kwargs["cand"].name))

    monkeypatch.setattr(cleanup.sq, "quota_operation_lock", op_lock)
    monkeypatch.setattr(cleanup, "_has_active_reservation", no_active)
    monkeypatch.setattr(cleanup, "_trash_and_delete", fake_trash)
    stats = {
        "trashed": 0, "deleted": 0, "failed": 0, "would_delete": 0,
        "skip_lock": 0, "skip_active": 0, "skip_fresh": 0,
        "reconcile_failed": 0,
    }

    asyncio.run(cleanup._cleanup_kind(
        object(), uid=7, label="user7", root=tmp_path, kind="training",
        retention=30, dry_run=False, stats=stats,
    ))

    assert events == [("lock", 7, "training", "sample"), ("trash", "sample")]


def test_trash_rename_triggers_reconcile_before_delete(monkeypatch, tmp_path):
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "data").write_bytes(b"x")
    order = []

    async def reconcile(*_args, **_kwargs):
        order.append("reconcile")
        return 0

    real_rmtree = cleanup.shutil.rmtree

    def remove(path):
        order.append("delete")
        return real_rmtree(path)

    monkeypatch.setattr(cleanup.sq, "reconcile_usage", reconcile)
    monkeypatch.setattr(cleanup.shutil, "rmtree", remove)
    stats = {
        "trashed": 0, "deleted": 0, "failed": 0, "would_delete": 0,
        "reconcile_failed": 0,
    }
    lock = sq.QuotaOperationLock(
        type("T", (), {"acquired": True})(), type("T", (), {"acquired": True})()
    )

    asyncio.run(cleanup._trash_and_delete(
        object(), uid=1, label="u", root=tmp_path, cand=sample,
        kind="training", dry_run=False, stats=stats, lock_context=lock,
    ))

    assert order == ["reconcile", "delete"]
    assert stats["trashed"] == 1
    assert stats["deleted"] == 1
