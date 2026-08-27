"""reconcile_needed 状态机的生产服务函数测试。"""
import asyncio

from app.services import storage_quota as sq
from models import StorageQuotaReservation, UserStorageQuota
from tests.test_storage_quota import FakeOperationLock, Memo, _install, _mk


def _run(coro):
    return asyncio.run(coro)


def test_mark_then_settle_clears_state_when_no_other_active(monkeypatch):
    memo = Memo()
    mod, session = _install(monkeypatch, memo, enabled=True, dry_run=False)
    quota = _mk(1, "training", used=0, reserved=10)
    memo.quotas[(1, "training")] = quota
    reservation = StorageQuotaReservation(
        reservation_id="r1", user_id=1, kind="training", sample_key="s",
        status="reserved", requested_bytes=10,
    )
    memo.reservations["r1"] = reservation
    lock = FakeOperationLock()

    async def run():
        await mod.mark_reconcile_needed(
            session, user_id=1, kind="training", reason="post_swap_pre_settle",
            sample_key="s", lock_context=lock,
        )
        assert quota.reconcile_needed is True
        await mod.settle_quota(
            session, reservation_id="r1", delta_bytes=10, user_id=1,
            kind="training", sample_key="s", lock_context=lock,
        )

    _run(run())
    assert quota.reconcile_needed is False
    assert quota.reconcile_reason is None
    assert quota.reconcile_sample_key is None
    assert quota.reconcile_marked_at is None


def test_settle_keeps_state_when_other_reservation_active(monkeypatch):
    memo = Memo()
    mod, session = _install(monkeypatch, memo, enabled=True, dry_run=False)
    quota = _mk(1, "training", used=0, reserved=20)
    quota.reconcile_needed = True
    quota.reconcile_reason = "post_swap_pre_settle"
    quota.reconcile_sample_key = "a"
    memo.quotas[(1, "training")] = quota
    for rid, sample in (("r1", "a"), ("r2", "b")):
        memo.reservations[rid] = StorageQuotaReservation(
            reservation_id=rid, user_id=1, kind="training", sample_key=sample,
            status="reserved", requested_bytes=10,
        )

    _run(mod.settle_quota(
        session, reservation_id="r1", delta_bytes=10, user_id=1,
        kind="training", sample_key="a", lock_context=FakeOperationLock(),
    ))

    assert quota.reconcile_needed is True
    assert quota.reconcile_reason == "post_swap_pre_settle"


def test_release_stored_marks_needed_but_pre_swap_release_does_not(monkeypatch):
    memo = Memo()
    mod, session = _install(monkeypatch, memo, enabled=True, dry_run=False)
    quota = _mk(1, "training", reserved=20)
    # 内存 mock 不经真实 INSERT，DB 列 default=False 不会自动应用，
    # 显式模拟已持久化的初始状态。
    quota.reconcile_needed = False
    memo.quotas[(1, "training")] = quota
    for rid in ("before", "after"):
        memo.reservations[rid] = StorageQuotaReservation(
            reservation_id=rid, user_id=1, kind="training", sample_key=rid,
            status="reserved", requested_bytes=10,
        )

    async def run():
        await mod.release_reservation(
            session, reservation_id="before", user_id=1, kind="training",
            sample_key="before", stored=False, lock_context=FakeOperationLock(),
        )
        assert quota.reconcile_needed is False
        await mod.release_reservation(
            session, reservation_id="after", user_id=1, kind="training",
            sample_key="after", stored=True, lock_context=FakeOperationLock(),
        )

    _run(run())
    assert quota.reconcile_needed is True
    assert quota.reconcile_reason == "released_after_store"
    assert quota.reconcile_sample_key == "after"


def test_mark_does_not_overwrite_unresolved_other_sample(monkeypatch):
    memo = Memo()
    mod, session = _install(monkeypatch, memo, enabled=True, dry_run=False)
    quota = _mk(1, "training")
    quota.reconcile_needed = True
    quota.reconcile_reason = "reservation_expired"
    quota.reconcile_sample_key = "sample-a"
    memo.quotas[(1, "training")] = quota

    _run(mod.mark_reconcile_needed(
        session, user_id=1, kind="training", reason="post_swap_pre_settle",
        sample_key="sample-b", lock_context=FakeOperationLock(),
    ))

    assert quota.reconcile_reason == "multiple_samples"
    assert quota.reconcile_sample_key is None


def test_reconcile_clears_all_state_fields(monkeypatch, tmp_path):
    memo = Memo()
    mod, session = _install(monkeypatch, memo, enabled=True, dry_run=True)
    quota = UserStorageQuota(
        user_id=1, kind="training", used_bytes=99, reserved_bytes=0,
        reconcile_needed=True, reconcile_reason="x", reconcile_sample_key="s",
        reconcile_marked_at=sq.now_utc(),
    )
    memo.quotas[(1, "training")] = quota
    monkeypatch.setattr(mod, "quota_sample_root", lambda kind, label: tmp_path)

    _run(mod.reconcile_usage(
        session, user_id=1, kind="training", storage_label="u",
        lock_context=FakeOperationLock(),
    ))

    assert quota.reconcile_needed is False
    assert quota.reconcile_reason is None
    assert quota.reconcile_sample_key is None
    assert quota.reconcile_marked_at is None
