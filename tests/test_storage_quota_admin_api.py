"""管理员存储配额总览接口的真实响应测试。"""
import asyncio
from datetime import datetime

from app.routes import admin as admin_routes
from models import User, UserStorageQuota


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def __init__(self, values, scalar=False):
        self.values = values
        self.scalar = scalar

    def scalars(self):
        return _Scalars(self.values)

    def all(self):
        return self.values


class _Session:
    def __init__(self, quotas, users):
        self.results = [_Result(quotas), _Result(users)]

    async def execute(self, _stmt):
        return self.results.pop(0)


def test_admin_storage_stats_exposes_reconcile_and_physical_fields(monkeypatch, tmp_path):
    marked = datetime(2026, 8, 27, 12, 0, 0)
    quota = UserStorageQuota(
        user_id=7,
        kind="training",
        used_bytes=100,
        reserved_bytes=20,
        quota_mb=1,
        reconcile_needed=True,
        reconcile_reason="reservation_expired",
        reconcile_sample_key="sample-x",
        reconcile_marked_at=marked,
    )
    session = _Session([quota], [(7, "user_u7")])

    from app.services import storage_quota as sq

    training_root = tmp_path / "training"
    detection_root = tmp_path / "detection"
    training_root.mkdir()
    detection_root.mkdir()
    recovery = training_root / "sample.backup-recovery-deadbeef"
    recovery.mkdir()
    (recovery / "old").write_bytes(b"abc")

    monkeypatch.setattr(
        sq,
        "quota_sample_root",
        lambda kind, _label: training_root if kind == "training" else detection_root,
    )
    monkeypatch.setattr(admin_routes, "STORAGE_TRAINING_CORPUS_DIR", tmp_path / "all-training")
    monkeypatch.setattr(admin_routes, "STORAGE_USERS_DIR", tmp_path / "all-users")

    body = asyncio.run(
        admin_routes.admin_storage_stats(
            _admin=User(id=1, role="admin"),
            db=session,
        )
    )

    assert body["reconcile_needed_user_count"] == 1
    assert body["top_users"][0]["reconcile_needed"] is True
    assert body["top_users"][0]["reconcile_reason"] == "reservation_expired"
    assert body["top_users"][0]["reconcile_sample_key"] == "sample-x"
    assert body["physical_orphans"]["recovery_backup_bytes"] == 3
    assert body["physical_orphans"]["recovery_backup_count"] == 1
