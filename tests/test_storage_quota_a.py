"""阶段 A：user-kind 行锁 + SAVEPOINT 唯一键兜底测试。

覆盖：
- row lock key 与 sample lock key 不同
- 唯一键 IntegrityError 用 SAVEPOINT(begin_nested)，外层 rollback 不得被调用
- IntegrityError 后重查既有行成功
- 锁连接：QuotaLockToken release 幂等、连接关闭、双重 release 安全
"""
import asyncio

import app.services.storage_quota as sq
from models import UserStorageQuota
from sqlalchemy.exc import IntegrityError


class Memo:
    pass


def test_quota_row_lock_key_differs_from_sample_lock_key():
    rk = sq._quota_row_lock_key(1, "training")
    sk = sq._lock_key(1, "training", "s")
    assert rk != sk
    assert sq._hash_lock_key(rk) != sq._hash_lock_key(sk)


class RecordingSession:
    """模拟真实唯一键冲突：flush 抛 IntegrityError，重查返回已有行。"""

    def __init__(self):
        self.nested = 0
        self.outer_rollback = 0
        self.flush_calls = 0
        self.existing = UserStorageQuota(
            user_id=1, kind="training", used_bytes=10, reserved_bytes=0
        )

    def begin_nested(self):
        self.nested += 1
        return _NestedCtx()

    async def rollback(self):
        self.outer_rollback += 1

    def add(self, _obj):
        pass

    async def flush(self):
        self.flush_calls += 1
        raise IntegrityError("INSERT", {}, Exception("duplicate key (simulated)"))


class _Scalars:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return _Scalars(self.value)


class _NestedCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_quota_insert_integrity_error_uses_savepoint_without_outer_rollback():
    sess = RecordingSession()
    calls = {"execute": 0}

    async def execute(_stmt):
        calls["execute"] += 1
        # 第一次查询模拟行不存在；冲突后的重查返回竞争者已创建的行。
        return _Result(None if calls["execute"] == 1 else sess.existing)

    sess.execute = execute
    qrow = asyncio.run(sq._get_or_create_quota_row_locked(sess, 1, "training"))

    assert qrow is sess.existing
    assert sess.nested == 1
    assert sess.flush_calls == 1
    assert sess.outer_rollback == 0
    assert calls["execute"] >= 2


def test_acquire_row_lock_releases_the_same_key_it_acquired(monkeypatch):
    calls = []

    class Scalar:
        def scalar(self):
            return 1

    class Conn:
        async def execute(self, _stmt, params):
            calls.append(params["k"])
            return Scalar()

        async def close(self):
            calls.append("closed")

    class Engine:
        async def connect(self):
            return Conn()

    monkeypatch.setattr(sq, "get_engine", lambda: Engine())

    async def run():
        token = await sq.acquire_quota_row_lock(user_id=1, kind="training")
        acquired_key = token.key
        await token.release()
        return acquired_key

    key = asyncio.run(run())
    assert calls == [key, key, "closed"]
    assert key == sq._hash_lock_key(sq._quota_row_lock_key(1, "training"))


def test_quota_row_lock_release_idempotent_conn_closed():
    """锁 token：release 后连接关闭、acquired=False，重复 release 安全。"""
    class FakeConn:
        def __init__(self):
            self.closed = False

        async def execute(self, *a, **k):
            return None

        async def close(self):
            self.closed = True

    async def run():
        conn = FakeConn()
        tok = sq.QuotaLockToken(None, user_id=1, kind="training", sample_key="s", acquired=True)
        tok.key = sq._hash_lock_key(sq._quota_row_lock_key(1, "training"))
        tok._conn = conn
        await tok.release()
        await tok.release()  # 二次
        return conn, tok

    conn, tok = asyncio.run(run())
    assert conn.closed is True
    assert tok.acquired is False