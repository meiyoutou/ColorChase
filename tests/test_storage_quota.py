"""存储配额服务层单元测试（不依赖真实 MySQL）。

用 monkeypatch 替换 sq 模块的 DB helper（_get_quota_row / _get_reservation /
_get_user_role / _mysql_lock / _mysql_unlock）为内存实现。session 用一个
TestSession：其 add() 按对象类型登记到 memo（配额行/预留行），helper 再返回
对象引用，因此服务层的属性读写（used_bytes/reserved_bytes/计数/status）会
自然回读内存状态。

覆盖验收项：
- 默认开关 OFF 时 reserve/settle/release/reconcile/reclaim 全 no-op、0 次 DB 写
- 管理员豁免与 is_admin_role 一致
- dry-run 超限放行并标 would_deny（计数+1）
- 强制模式多文件合计一次性 reserve 无法绕过
- 净差额结算（正/负、used 底 0）
- 两并发预留不共同越配额
- 写盘失败 release 后 reserved 回退
- reconcile 使用真实 paths helper
- 清理整目录、不跟随 symlink、不碰隐藏/非样本目录
- 目录过期判定用「目录内最大 mtime」
- RETENTION=0 时清理绝不删除
"""
import asyncio
import os
import time
from pathlib import Path

import pytest

from app import settings
import app.services.storage_quota as sq
from models import StorageQuotaReservation, UserStorageQuota


class Memo:
    def __init__(self):
        self.quotas = {}       # (user_id, kind) -> UserStorageQuota
        self.reservations = {}  # reservation_id -> StorageQuotaReservation
        self.roles = {}         # user_id -> role
        self.writes = 0


class FakeSession:
    def __init__(self, memo):
        self.memo = memo

    def add(self, obj):
        self.memo.writes += 1
        if isinstance(obj, UserStorageQuota):
            self.memo.quotas[(obj.user_id, obj.kind)] = obj
        elif isinstance(obj, StorageQuotaReservation):
            self.memo.reservations[obj.reservation_id] = obj

    async def commit(self):
        return None


def _install(monkeypatch, memo, *, enabled, dry_run, quota_mb=1):
    """安装内存 DB helper 并 patch sq 模块常量，返回 (mod, session)。

    注意：sq 模块通过 `from app.settings import STORAGE_QUOTA_ENABLED` 等把常量
    绑定为自身命名空间的全局名，因此必须 patch `sq.<NAME>`（而不是 settings）；
    且不 reload，否则会覆盖 monkeypatch 注入的 DB helper。
    """

    async def get_quota_row(session, user_id, kind):
        return memo.quotas.get((user_id, kind))

    async def get_reservation(session, reservation_id):
        return memo.reservations.get(reservation_id)

    async def get_user_role(session, user_id):
        return memo.roles.get(user_id)

    async def mysql_lock(session, key, timeout=5.0):
        return True

    async def mysql_unlock(session, key):
        pass

    monkeypatch.setattr(sq, "_get_quota_row", get_quota_row)
    monkeypatch.setattr(sq, "_get_reservation", get_reservation)
    monkeypatch.setattr(sq, "_get_user_role", get_user_role)
    monkeypatch.setattr(sq, "_mysql_lock", mysql_lock)
    monkeypatch.setattr(sq, "_mysql_unlock", mysql_unlock)

    monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", enabled)
    monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", dry_run)
    monkeypatch.setattr(sq, "TRAINING_QUOTA_MB", quota_mb)
    monkeypatch.setattr(sq, "DETECTION_QUOTA_MB", quota_mb)

    return sq, FakeSession(memo)


def _mk(user_id=1, kind="training", used=0, reserved=0, quota_mb=None, reconciled=True):
    return UserStorageQuota(
        user_id=user_id, kind=kind, used_bytes=used, reserved_bytes=reserved, quota_mb=quota_mb,
        reconciled_at=sq.now_utc() if reconciled else None,
    )


# ---- 1) 默认开关 OFF 时绝对 no-op、无 DB 写 ----
def test_off_is_total_noop(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=False, dry_run=True)
    before = sess.memo.writes

    async def run():
        out = await mod.reserve_quota(
            sess, user_id=1, role="user", storage_label=None, kind="training",
            sample_key="s", incoming_bytes=10 ** 9,
        )
        assert out.allowed is True and out.reason == "disabled"
        await mod.settle_quota(
            sess, reservation_id="x", delta_bytes=10, user_id=1,
            kind="training", sample_key="s",
        )
        await mod.release_reservation(
            sess, reservation_id="x", user_id=1, kind="training", sample_key="s"
        )
        await mod.reconcile_usage(sess, user_id=1, kind="training", storage_label="u1")
        await mod.reclaim_expired_reservations(sess)

    asyncio.run(run())
    assert sess.memo.writes == before


@pytest.fixture
def memo():
    return Memo()


def test_admin_exempt(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=True)
    memo.roles[1] = "super_admin"

    async def run():
        return await mod.reserve_quota(
            sess, user_id=1, role="user", storage_label=None, kind="training",
            sample_key="s", incoming_bytes=10 ** 9,
        )

    out = asyncio.run(run())
    assert out.allowed is True
    assert out.reason == "admin_exempt"
    assert out.would_deny is False
    # 管理员也会计量用量（观察模式有统计价值）——因此记录行会创建
    assert memo.quotas[(1, "training")].used_bytes == 10 ** 9


# ---- 3) dry-run 超限放行并标 would_deny（计数+1）----
def test_dry_run_would_deny(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=True, quota_mb=1)
    memo.quotas[(1, "training")] = _mk(1, "training", used=1024 * 1024)  # 已满

    async def run():
        return await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=1,
        )

    out = asyncio.run(run())
    assert out.allowed is True
    assert out.would_deny is True
    q = memo.quotas[(1, "training")]
    assert q.would_deny_count == 1
    assert q.last_would_deny_at is not None


# ---- 4) 强制模式：多文件合计一次性 reserve 无法绕过 ----
def test_multi_file_cannot_bypass(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=False, quota_mb=1)
    memo.quotas[(1, "training")] = _mk(1, "training", used=0, reconciled=True)

    async def run():
        first = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=900000,
        )
        assert first.allowed is True
        assert first.reservation_id is not None
        second = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=200000,  # 累计 1.1MB
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "quota_exceeded"
    assert memo.quotas[(1, "training")].denied_count == 1
    # 第一个预留增加了 reserved_bytes，且不越过
    assert memo.quotas[(1, "training")].reserved_bytes == 900000


# ---- 5) 净差额结算（正差额）----
def test_settle_positive_delta(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=False)
    memo.quotas[(1, "training")] = _mk(1, "training", used=100)

    async def run():
        out = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=90,
        )
        after = await mod.settle_quota(
            sess, reservation_id=out.reservation_id, delta_bytes=50,
            user_id=1, kind="training", sample_key="s",
        )
        return out, after

    out, new_used = asyncio.run(run())
    assert new_used == 150
    q = memo.quotas[(1, "training")]
    assert q.used_bytes == 150
    assert q.reserved_bytes == 0  # 预留已扣


# ---- 5b) 负差额（覆盖变小）、used 底 0 ----
def test_settle_negative_delta_floor_zero(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=False)
    memo.quotas[(1, "training")] = _mk(1, "training", used=50)

    async def run():
        out = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=10,
        )
        return await mod.settle_quota(
            sess, reservation_id=out.reservation_id, delta_bytes=-200,
            user_id=1, kind="training", sample_key="s",
        )

    after = asyncio.run(run())
    assert after == 0
    assert memo.quotas[(1, "training")].used_bytes == 0


# ---- 6) 两并发预留不共同占满 ----
def test_concurrent_not_exceed(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=False, quota_mb=1)
    memo.quotas[(1, "training")] = _mk(1, "training", used=0, reconciled=True)

    async def run():
        o1 = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=700000,
        )
        o2 = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=700000,
        )
        return o1, o2

    o1, o2 = asyncio.run(run())
    assert o1.allowed is True
    assert o2.allowed is False
    assert o2.reason == "quota_exceeded"


# ---- 7) 写盘失败 release 后 reserved 回退 ----
def test_release_on_failure(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=False)
    memo.quotas[(1, "training")] = _mk(1, "training", used=0, reserved=0)

    async def run():
        out = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=500,
        )
        assert memo.quotas[(1, "training")].reserved_bytes == 500
        await mod.release_reservation(
            sess, reservation_id=out.reservation_id,
            user_id=1, kind="training", sample_key="s",
        )
        assert memo.quotas[(1, "training")].reserved_bytes == 0
        assert memo.reservations[out.reservation_id].status == "released"

    asyncio.run(run())


# ---- 8. settle 幂等：第二次调用不再重复结算 ----
def test_settle_idempotent(monkeypatch, memo):
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=False)
    memo.quotas[(1, "training")] = _mk(1, "training", used=10)

    async def run():
        out = await mod.reserve_quota(
            sess, user_id=1, role="user", kind="training",
            sample_key="s", incoming_bytes=5,
        )
        a1 = await mod.settle_quota(
            sess, reservation_id=out.reservation_id, delta_bytes=7,
            user_id=1, kind="training", sample_key="s",
        )
        a2 = await mod.settle_quota(
            sess, reservation_id=out.reservation_id, delta_bytes=7,
            user_id=1, kind="training", sample_key="s",
        )
        return a1, a2

    a1, a2 = asyncio.run(run())
    assert a1 == 17
    assert a2 == 7  # 幂等：第二次已 settled，返回已结算的 settled_bytes(=7) 而非重复 +7
    assert memo.quotas[(1, "training")].used_bytes == 17


# ---- 9. reconcile 使用真实 paths helper ----
def test_reconcile_uses_real_paths(monkeypatch, memo, tmp_path):
    def mkdir(p):
        return p

    import app.services.paths as paths
    storage = tmp_path / "storage"
    training = storage / "training" / "corpus"
    sample = training / "user_u1" / "abc"
    sample.mkdir(parents=True)
    (sample / "a.jpg").write_bytes(b"x" * 100)

    monkeypatch.setattr(
        paths, "_training_corpus_dir_for_label",
        lambda label: training / label,
    )
    mod, sess = _install(monkeypatch, memo, enabled=True, dry_run=True, quota_mb=10)

    async def run():
        return await mod.reconcile_usage(
            sess, user_id=1, kind="training", storage_label="user_u1"
        )

    total = asyncio.run(run())
    assert total == 100
    assert memo.quotas[(1, "training")].used_bytes == 100
    assert memo.quotas[(1, "training")].reconciled_at is not None


# ---- 10. 清理：整目录候选 / 忽略隐藏与文件 / 不跟 symlink ----
def test_cleanup_candidates(tmp_path):
    good = tmp_path / "abc123"
    good.mkdir()
    (good / "a.jpg").write_bytes(b"a")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "b.jpg").write_bytes(b"b")
    plain_file = tmp_path / "notdir.jpg"
    plain_file.write_bytes(b"x")

    cands = sq.sample_subdir_candidates(tmp_path)
    names = {p.name for p in cands}
    assert "abc123" in names
    assert ".hidden" not in names
    assert "notdir.jpg" not in names


def test_expired_zero_retention_never_deletes(tmp_path):
    d = tmp_path / "abc"
    d.mkdir()
    f = d / "a"
    f.write_bytes(b"x")
    old = time.time() - 100 * 86400
    os.utime(f, (old, old))
    assert sq.expired_sample_dirs(tmp_path, retention_days=0) == []
    assert sq.expired_sample_dirs(tmp_path, retention_days=-1) == []


def test_expired_uses_directory_max_mtime(tmp_path):
    d = tmp_path / "abc"
    d.mkdir()
    old_d = time.time() - 400 * 86400

    # 目录内只有 400 天前的旧文件 -> 过期
    f_old = d / "old"
    f_old.write_bytes(b"x")
    os.utime(f_old, (old_d, old_d))
    assert sq.expired_sample_dirs(tmp_path, retention_days=30) == [d]

    # 再放入一个最新文件 -> 目录内 max mtime 变新 -> 不过期
    late = d / "just_now"
    late.write_bytes(b"z")
    assert sq.expired_sample_dirs(tmp_path, retention_days=30) == []

    # 移除最新文件 -> 只剩旧文件 -> 再次过期
    late.unlink()
    assert sq.expired_sample_dirs(tmp_path, retention_days=30) == [d]


def test_latest_mtime_not_follow_symlink(tmp_path):
    d = tmp_path / "abc"
    d.mkdir()
    f = d / "a"
    f.write_bytes(b"x")
    old = time.time() - 300 * 86400
    os.utime(f, (old, old))
    target = tmp_path / "target"
    target.write_bytes(b"y")
    os.utime(target, (time.time(), time.time()))
    try:
        (d / "link").symlink_to(str(target))
    except OSError:
        return  # 平台不支持 symlink 则跳过
    late = sq.latest_mtime(d)
    assert late is not None
    # 不应跟随 symlink 取到 now；应仍是 ~300 天前的 old
    assert late < time.time() - 100 * 86400