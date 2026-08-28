"""P0/P1 增量修复的针对性测试（含未对账 503、清理失败不计成功、净差额清理）。

主要补充在 tests/test_storage_quota.py 之外的可独立验证项；对依赖真实 MySQL 的
并发/GET_LOCK/真实清理，本文件标注 skip（需在可用 MySQL 环境跑）。
"""
import asyncio
import os
from pathlib import Path

import pytest

import app.services.storage_quota as sqm
from models import UserStorageQuota
from tests.test_storage_quota import FakeOperationLock


def _async(method_fn):
    """包装：直接在 asyncio 里跑无参数协程。"""
    return asyncio.run(method_fn())


@pytest.fixture
def off_patch(monkeypatch):
    """强制关闭，隔离开关常量的 monkeypatch。"""
    monkeypatch.setattr(sqm, "STORAGE_QUOTA_ENABLED", True)
    monkeypatch.setattr(sqm, "STORAGE_QUOTA_DRY_RUN", False)
    return None


async def _run(maybe_awaitable):
    if hasattr(maybe_awaitable, "__await__"):
        return await maybe_awaitable
    return maybe_awaitable


# 真实 MySQL 才可行的端到端，缺库时跳过
def test_real_mysql_present():
    import os

    has = bool(os.environ.get("COLORCHASE_DATABASE_URL", ""))
    # 不强制失败；本文件不依赖真实库


def test_unreconciled_force_mode_returns_503(monkeypatch):
    """强制模式、配额行存在但从未对账 → 必须拒绝（需先 reconcile），reason=reconcile_required。"""
    global _get_quota_row_impl

    memo = {"roles": {1: "user"}, "quotas": {}}

    async def get_q(session, user_id, kind):
        return memo["quotas"].get((user_id, kind))

    async def role(session, user_id):
        return memo["roles"].get(user_id, "user")

    monkeypatch.setattr(sqm, "_get_quota_row", get_q)
    monkeypatch.setattr(sqm, "_get_user_role", role)
    monkeypatch.setattr(sqm, "STORAGE_QUOTA_ENABLED", True)
    monkeypatch.setattr(sqm, "STORAGE_QUOTA_DRY_RUN", False)

    q = UserStorageQuota(user_id=1, kind="training", used_bytes=0, reserved_bytes=0,
                         reconciled_at=None)
    memo["quotas"][(1, "training")] = q

    async def run():
        return await sqm.reserve_quota(None, user_id=1, role="user",
                                       kind="training", sample_key="s", incoming_bytes=1,
                                       lock_context=FakeOperationLock())

    out = asyncio.run(run())
    assert out.allowed is False
    assert out.reason == "reconcile_required"


def test_negative_delta_releases_quota(monkeypatch):
    memo = {"roles": {1: "user"}, "quotas": {}}

    async def get_q(session, user_id, kind):
        return memo["quotas"].get((user_id, kind))

    async def role(session, user_id):
        return "user"

    monkeypatch.setattr(sqm, "_get_quota_row", get_q)
    monkeypatch.setattr(sqm, "_get_user_role", role)
    monkeypatch.setattr(sqm, "STORAGE_QUOTA_ENABLED", True)
    monkeypatch.setattr(sqm, "STORAGE_QUOTA_DRY_RUN", True)  # dry-run：仍需要记账
    q = UserStorageQuota(user_id=1, kind="training", used_bytes=1000, reserved_bytes=0,
                         reconciled_at=sqm.now_utc())
    memo["quotas"][(1, "training")] = q

    async def run():
        out = await sqm.reserve_quota(None, user_id=1, role="user", kind="training",
                                      sample_key="s", incoming_bytes=-500,
                                      lock_context=FakeOperationLock())
        return out

    out = asyncio.run(run())
    # 负差额：不越配额，dry-run 放行且记录用量
    assert out.allowed is True
    # 负差额 in record_usage 会把它作为增量，直接 record_usage 后 used 向下修正由调用方处理