"""阶段 B：真实组合锁上下文 quota_operation_lock 测试。

直接 import 生产函数 quota_operation_lock。函数缺失时收集即失败（红）。
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.services.storage_quota as sq
from app.services.storage_quota import quota_operation_lock  # noqa: F401


def _tok(acquired=True, name=""):
    tok = SimpleNamespace(acquired=acquired, name=name)
    tok.release = _mk_release(tok)
    return tok


def _mk_release(tok):
    async def release():
        tok.acquired = False
    return release


def _run(coro):
    return asyncio.run(coro)


def test_quota_operation_lock_acquires_row_then_sample(monkeypatch):
    order = []

    async def fake_row(user_id, kind, **kw):
        order.append("row")
        return _tok(True, "row")

    async def fake_sample(user_id, kind, sample_key, **kw):
        order.append("sample")
        return _tok(True, "sample")

    monkeypatch.setattr(sq, "acquire_quota_row_lock", fake_row)
    monkeypatch.setattr(sq, "acquire_sample_lock", fake_sample)

    async def run():
        async with quota_operation_lock(1, "training", "s"):
            return list(order)

    assert _run(run()) == ["row", "sample"]


def test_quota_operation_lock_releases_reverse_order(monkeypatch):
    seq = []

    def rec(tok, name):
        async def release():
            seq.append(name)
            tok.acquired = False
        return release

    async def fake_row(user_id, kind, **kw):
        t = _tok(True, "row")
        t.release = rec(t, "row")
        return t

    async def fake_sample(user_id, kind, sample_key, **kw):
        t = _tok(True, "sample")
        t.release = rec(t, "sample")
        return t

    monkeypatch.setattr(sq, "acquire_quota_row_lock", fake_row)
    monkeypatch.setattr(sq, "acquire_sample_lock", fake_sample)

    async def run():
        async with quota_operation_lock(1, "training", "s"):
            pass

    _run(run())
    assert seq == ["sample", "row"]


def test_quota_operation_lock_releases_row_when_sample_fails(monkeypatch):
    row_released = []

    def rec(tok):
        async def release():
            row_released.append(1)
            tok.acquired = False
        return release

    async def fake_row(user_id, kind, **kw):
        t = _tok(True, "row")
        t.release = rec(t)
        return t

    async def fake_sample(user_id, kind, sample_key, **kw):
        raise RuntimeError("sample lock fail")

    monkeypatch.setattr(sq, "acquire_quota_row_lock", fake_row)
    monkeypatch.setattr(sq, "acquire_sample_lock", fake_sample)

    async def run():
        with pytest.raises(RuntimeError):
            async with quota_operation_lock(1, "training", "s"):
                pass

    _run(run())
    assert row_released == [1]


def test_quota_operation_lock_reports_unavailable_when_row_missing(monkeypatch):
    async def fake_row(user_id, kind, **kw):
        return _tok(False, "row")

    async def fake_sample(user_id, kind, sample_key, **kw):
        raise AssertionError("sample must not be requested")

    monkeypatch.setattr(sq, "acquire_quota_row_lock", fake_row)
    monkeypatch.setattr(sq, "acquire_sample_lock", fake_sample)

    async def run():
        try:
            async with quota_operation_lock(1, "training", "s"):
                pytest.fail("must not enter critical section")
        except sq.QuotaLockUnavailable:
            return "unavailable"

    assert _run(run()) == "unavailable"


def test_quota_operation_lock_releases_row_when_sample_release_fails(monkeypatch):
    events = []

    async def fake_row(user_id, kind, **kw):
        token = _tok(True, "row")

        async def release():
            events.append("row")
            token.acquired = False

        token.release = release
        return token

    async def fake_sample(user_id, kind, sample_key, **kw):
        token = _tok(True, "sample")

        async def release():
            events.append("sample")
            token.acquired = False
            raise OSError("close failed")

        token.release = release
        return token

    monkeypatch.setattr(sq, "acquire_quota_row_lock", fake_row)
    monkeypatch.setattr(sq, "acquire_sample_lock", fake_sample)

    async def run():
        with pytest.raises(OSError):
            async with quota_operation_lock(1, "training", "s"):
                pass

    _run(run())
    assert events == ["sample", "row"]


def test_quota_operation_lock_exposes_tokens(monkeypatch):
    async def fake_row(user_id, kind, **kw):
        return _tok(True, "row")

    async def fake_sample(user_id, kind, sample_key, **kw):
        return _tok(True, "sample")

    monkeypatch.setattr(sq, "acquire_quota_row_lock", fake_row)
    monkeypatch.setattr(sq, "acquire_sample_lock", fake_sample)

    async def run():
        async with quota_operation_lock(1, "training", "s") as op:
            return op

    op = _run(run())
    assert op.acquired is True
    assert op.row_token.name == "row"
    assert op.sample_token.name == "sample"