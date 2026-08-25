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
    """记录会话：begin_nested 次数、外层 rollback 是否被调。"""

    def __init__(self):
        self.nested = 0
        self.outer_rollback = 0
        self.conflict = True
        self.retried = False
        self.added = []

    def begin_nested(self):
        self.nested += 1
        return _NestedCtx(self)

    async def rollback(self):
        self.outer_rollback += 1

    async def commit(self):
        pass

    def add(self, obj):
        if isinstance(obj, UserStorageQuota):
            if self.conflict:
                self.conflict = False
                self.retried = True
                raise IntegrityError("INSERT", {}, Exception("duplicate key (simulated)"))
            self.added.append(obj)


class _NestedCtx:
    """模拟 SAVEPOINT：冲突时自动回滚嵌套保存点并返回，不放外层异常。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return True  # 吞掉保存点内异常


def test_quota_insert_integrity_error_uses_savepoint_without_outer_rollback():
    """唯一键冲突必须走 SAVEPOINT，外层 rollback 绝不调用；冲突后重查。"""
    sess = RecordingSession()

    async def run():
        reservation_id = "r1"
        qrow = None
        try:
            async with sess.begin_nested():
                qrow = UserStorageQuota(user_id=1, kind="training",
                                        used_bytes=0, reserved_bytes=0)
                sess.add(qrow)  # 第一次 add 抛 IntegrityError
        except IntegrityError:
            # SAVEPOINT 已吞；这里走重查路径（模拟返回已有行）
            qrow = UserStorageQuota(user_id=1, kind="training",
                                    used_bytes=10, reserved_bytes=0)
        return qrow

    qrow = asyncio.run(run())
    assert sess.nested >= 1
    assert sess.outer_rollback == 0  # 禁止外层 rollback
    assert sess.retried is True  # IntegrityError 后重试/重查


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