"""存储配额服务层（异步，AsyncSession + select）。

设计原则：
- 总开关 {STORAGE_QUOTA_ENABLED} 关闭时所有对外函数立即 no-op，对现有行为零影响。
- dry-run（{STORAGE_QUOTA_DRY_RUN}=True）：统计用量与 would_deny 计数，但绝不拒绝、绝不预留、绝不删除。
- 管理员（is_admin_role，基于 DB User.role）豁免，但用量仍统计。
- 同一 user/kind/sample_key 用命名锁 + SELECT ... FOR UPDATE 串行化，防止并发共同越过配额。
- reservation_id 幂等 settle/release，支持过期预留回收。

DB 访问全部收敛到模块级 helper，便于测试在无 MySQL 环境替换。
"""
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select, text

from app.services.auth_utils import is_admin_role
from app.settings import (
    DETECTION_QUOTA_MB,
    STORAGE_QUOTA_DRY_RUN,
    STORAGE_QUOTA_ENABLED,
    STORAGE_QUOTA_KINDS,
    TRAINING_QUOTA_MB,
)
from database import get_engine
from models import (
    StorageQuotaReservation,
    User,
    UserStorageQuota,
)
from sqlalchemy.exc import IntegrityError


def now_utc() -> datetime:
    return datetime.utcnow()


def is_quota_enabled() -> bool:
    """配额/清理总开关。默认关闭。"""
    return bool(STORAGE_QUOTA_ENABLED)


def is_dry_run() -> bool:
    """观测模式。默认开启（不拒绝、不预留、不删除）。"""
    return bool(STORAGE_QUOTA_DRY_RUN)


# ---------------------------------------------------------------------------
# 输出对象与纯逻辑辅助
# ---------------------------------------------------------------------------
@dataclass
class ReserveOutcome:
    allowed: bool
    reservation_id: Optional[str] = None
    would_deny: bool = False
    dry_run_fail_open: bool = False
    used_bytes: int = 0
    quota_bytes: int = 0
    reason: Optional[str] = None  # "disabled"|"admin_exempt"|"quota_exceeded"|None


def validate_kind(kind: str) -> str:
    if kind not in STORAGE_QUOTA_KINDS:
        raise ValueError(f"invalid quota kind: {kind}")
    return kind


def default_quota_bytes(kind: str) -> int:
    validate_kind(kind)
    mb = TRAINING_QUOTA_MB if kind == "training" else DETECTION_QUOTA_MB
    return mb * 1024 * 1024


def _clamp0(value) -> int:
    return max(int(value or 0), 0)


def would_exceed(used_bytes, reserved_bytes, incoming_bytes, quota_bytes) -> bool:
    return (used_bytes + reserved_bytes + incoming_bytes) > quota_bytes


def upsert_delta(used_bytes, delta_bytes) -> int:
    """按净差额（可负）更新用量；下限 0。"""
    return _clamp0(used_bytes + delta_bytes)


# ---------------------------------------------------------------------------
# DB helper（真实实现走 SQLAlchemy AsyncSession；测试可替换）
# ---------------------------------------------------------------------------
async def _get_quota_row(session, user_id: int, kind: str):
    validate_kind(kind)
    stmt = (
        select(UserStorageQuota)
        .where(UserStorageQuota.user_id == user_id, UserStorageQuota.kind == kind)
        .with_for_update()
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _get_reservation(session, reservation_id: str):
    stmt = (
        select(StorageQuotaReservation)
        .where(StorageQuotaReservation.reservation_id == reservation_id)
        .with_for_update()
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _has_other_active_reservations(
    session, user_id: int, kind: str, reservation_id: Optional[str] = None
) -> bool:
    stmt = select(StorageQuotaReservation.reservation_id).where(
        StorageQuotaReservation.user_id == user_id,
        StorageQuotaReservation.kind == kind,
        StorageQuotaReservation.status == "reserved",
    )
    if reservation_id:
        stmt = stmt.where(StorageQuotaReservation.reservation_id != reservation_id)
    result = await session.execute(stmt.limit(1))
    return result.first() is not None


async def _get_user_role(session, user_id: int) -> Optional[str]:
    stmt = select(User.role).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _hash_lock_key(key: str) -> str:
    """GET_LOCK key 上限 64 字符，用 SHA-256 截断到 56 前缀 + 语义摘要。"""
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"sq_{digest[:56]}"


class QuotaLockToken:
    """上传全程持有的独立 AsyncConnection + GET_LOCK 令牌。

    GET_LOCK / RELEASE_LOCK 必须在同一连接上执行；本 token 自持一个独立
    AsyncConnection（不从请求会话借用），其生命周期覆盖 reserve→写盘→settle/release，
    结束后显式 close。
    """

    def __init__(self, engine, *, user_id, kind, sample_key, acquired: bool = False):
        self._engine = engine
        self._conn = None
        self.key = _hash_lock_key(_lock_key(user_id, kind, sample_key))
        self.acquired = acquired

    async def release(self) -> None:
        conn = self._conn
        was_acquired = self.acquired
        self._conn = None
        self.acquired = False
        try:
            if conn is not None and was_acquired:
                await conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": self.key})
        finally:
            if conn is not None:
                await conn.close()


async def acquire_sample_lock(
    *, user_id: int, kind: str, sample_key: str, timeout: float = 5.0
) -> QuotaLockToken:
    """在独立 AsyncConnection 上获取 GET_LOCK（锁令牌持有该连接）。

    获取失败或 engine.connect() 异常都返回 acquired=False（不抛到路由变 500）。
    """
    engine = get_engine()
    token = QuotaLockToken(engine, user_id=user_id, kind=kind, sample_key=sample_key)
    try:
        from sqlalchemy.ext.asyncio import AsyncConnection

        conn = await engine.connect()
        token._conn = conn
        raw_key = _lock_key(user_id, kind, sample_key)
        result = await conn.execute(text("SELECT GET_LOCK(:k, :t)"), {"k": _hash_lock_key(raw_key), "t": timeout})
        token.acquired = bool(result.scalar())
        if not token.acquired:
            # GET_LOCK 返回 0（锁超时未获）应立即关闭连接，不泄漏
            await conn.close()
            token._conn = None
    except Exception:
        # engine.connect() 异常或 GET_LOCK 异常：返回 acquired=False，不 500
        token.acquired = False
        conn = token._conn
        token._conn = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
    return token


async def acquire_quota_row_lock(
    *, user_id: int, kind: str, timeout: float = 5.0
) -> QuotaLockToken:
    """user-kind 级 quota 行锁：保护 qrow 首次创建与关键增量更新。

    独立 AsyncConnection + QuotaLockToken 管理连接生命周期；key = hash("storage_quota_row:{user,kind}")，
    与 sample lock key 不同。锁顺序统一为：先 quota-row lock，再 sample lock。
    """
    engine = get_engine()
    token = QuotaLockToken(engine, user_id=user_id, kind=kind, sample_key="", acquired=False)
    try:
        from sqlalchemy.ext.asyncio import AsyncConnection

        conn = await engine.connect()
        token._conn = conn
        raw_key = _quota_row_lock_key(user_id, kind)
        token.key = _hash_lock_key(raw_key)
        result = await conn.execute(
            text("SELECT GET_LOCK(:k, :t)"), {"k": token.key, "t": timeout}
        )
        token.acquired = bool(result.scalar())
        if not token.acquired:
            await conn.close()
            token._conn = None
    except Exception:
        token.acquired = False
        conn = token._conn
        token._conn = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
    return token


class QuotaLockUnavailable(Exception):
    """组合锁上下文无法取得所需锁。"""


class QuotaOperationLock:
    """组合锁结果：暴露 acquired / row_token / sample_token。"""

    def __init__(self, row_token, sample_token):
        self.row_token = row_token
        self.sample_token = sample_token
        self.acquired = bool(row_token is not None and row_token.acquired
                             and sample_token is not None and sample_token.acquired)


class _QuotaOperationCtx:
    def __init__(self, *, user_id, kind, sample_key, timeout):
        self._user_id = user_id
        self._kind = kind
        self._sample_key = sample_key
        self._timeout = timeout
        self._row_token = None
        self._sample_token = None
        self.result = None

    async def __aenter__(self):
        row = await acquire_quota_row_lock(user_id=self._user_id, kind=self._kind, timeout=self._timeout)
        self._row_token = row
        if not row.acquired:
            await row.release()
            raise QuotaLockUnavailable("quota row lock unavailable")
        try:
            samp = await acquire_sample_lock(
                user_id=self._user_id, kind=self._kind, sample_key=self._sample_key, timeout=self._timeout
            )
        except Exception:
            # sample 获取异常：先释放已取 row（逆序），再向上抛
            await row.release()
            raise
        self._sample_token = samp
        if not samp.acquired:
            await samp.release()
            await row.release()
            raise QuotaLockUnavailable("sample lock unavailable")
        op = QuotaOperationLock(row, samp)
        self.result = op
        return op

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._sample_token is not None:
            await self._sample_token.release()
        if self._row_token is not None:
            await self._row_token.release()
        return False


def quota_operation_lock(user_id: int, kind: str, sample_key: str, timeout: float = 5.0):
    return _QuotaOperationCtx(user_id=user_id, kind=kind, sample_key=sample_key, timeout=timeout)


def _new_reservation_id() -> str:
    return _uuid.uuid4().hex


# ---------------------------------------------------------------------------
# 核心对外 API
# ---------------------------------------------------------------------------
async def _reserve_quota_locked(
    session,
    *,
    user_id: int,
    role: Optional[str],
    kind: str,
    sample_key: str,
    incoming_bytes: int,
    dry_run: bool,
    has_lock: bool,
) -> "ReserveOutcome":
    """在已持锁或 fail-open 条件下执行配额预留。"""
    try:
        db_role = await _get_user_role(session, user_id)
    except Exception:
        db_role = role
    if is_admin_role(db_role):
        return ReserveOutcome(allowed=True, reason="admin_exempt")

    try:
        qrow = await _get_quota_row(session, user_id, kind)
        quota_bytes = default_quota_bytes(kind)
        if qrow is not None and getattr(qrow, "quota_mb", None) is not None:
            quota_bytes = _clamp0(qrow.quota_mb) * 1024 * 1024
        used = _clamp0(getattr(qrow, "used_bytes", 0))
        reserved = _clamp0(getattr(qrow, "reserved_bytes", 0))

        needs_reconcile = (
            qrow is None
            or getattr(qrow, "reconciled_at", None) is None
            or bool(getattr(qrow, "reconcile_needed", False))
        )
        if not dry_run and (not has_lock or needs_reconcile):
            reason = "reconcile_required" if needs_reconcile else "quota_unavailable"
            return ReserveOutcome(allowed=False, reason=reason, used_bytes=used, quota_bytes=quota_bytes)

        exceed = would_exceed(used, reserved, incoming_bytes, quota_bytes)
        if not exceed:
            if dry_run:
                return ReserveOutcome(allowed=True, used_bytes=used, quota_bytes=quota_bytes)
            reservation_id = _new_reservation_id()
            qrow = await _get_or_create_quota_row_locked(session, user_id, kind)
            qrow.reserved_bytes = _clamp0(qrow.reserved_bytes + incoming_bytes)
            session.add(StorageQuotaReservation(
                reservation_id=reservation_id, user_id=user_id, kind=kind, sample_key=sample_key,
                status="reserved", requested_bytes=incoming_bytes, occupied_bytes=0,
                settled_bytes=0, expires_at=now_utc() + timedelta(minutes=30),
            ))
            await session.commit()
            return ReserveOutcome(allowed=True, reservation_id=reservation_id, used_bytes=used, quota_bytes=quota_bytes)

        if dry_run:
            await _count_stat(session, user_id, kind, "would_deny_count", "last_would_deny_at")
            return ReserveOutcome(allowed=True, would_deny=True, used_bytes=used, quota_bytes=quota_bytes)

        await _count_stat(session, user_id, kind, "denied_count", "last_denied_at")
        return ReserveOutcome(allowed=False, used_bytes=used, quota_bytes=quota_bytes, reason="quota_exceeded")
    except Exception:
        if dry_run:
            return ReserveOutcome(allowed=True, dry_run_fail_open=True, used_bytes=0, quota_bytes=default_quota_bytes(kind))
        raise


async def reserve_quota(
    session,
    *,
    user_id: int,
    role: Optional[str],
    storage_label: Optional[str] = None,
    kind: str,
    sample_key: str,
    incoming_bytes: int,
    lock_context: Optional[QuotaOperationLock] = None,
) -> "ReserveOutcome":
    """配额预留。

    - lock_context.acquired=True：直接复用，不再加锁。
    - 无 lock_context：独立调用取 quota-row lock（不取 sample），包裹后释放。
    - context 存在但 acquired=False：forceQuotaLockUnavailable；dry-run fail-open。
    """
    validate_kind(kind)
    if not is_quota_enabled():
        return ReserveOutcome(allowed=True, reason="disabled")

    incoming_bytes = _clamp0(incoming_bytes)
    dry_run = is_dry_run()

    if lock_context is not None:
        if getattr(lock_context, "acquired", False):
            return await _reserve_quota_locked(
                session, user_id=user_id, role=role, kind=kind, sample_key=sample_key,
                incoming_bytes=incoming_bytes, dry_run=dry_run, has_lock=True,
            )
        if not dry_run:
            raise QuotaLockUnavailable("quota lock context unavailable in reserve")
        return ReserveOutcome(allowed=True, dry_run_fail_open=True, used_bytes=0,
                              quota_bytes=default_quota_bytes(kind))

    row_tok = await acquire_quota_row_lock(user_id=user_id, kind=kind)
    try:
        if not row_tok.acquired:
            if dry_run:
                return ReserveOutcome(allowed=True, dry_run_fail_open=True, used_bytes=0,
                                      quota_bytes=default_quota_bytes(kind))
            raise QuotaLockUnavailable("quota row lock unavailable")
        return await _reserve_quota_locked(
            session, user_id=user_id, role=role, kind=kind, sample_key=sample_key,
            incoming_bytes=incoming_bytes, dry_run=dry_run, has_lock=True,
        )
    finally:
        await row_tok.release()


def _lock_key(user_id: int, kind: str, sample_key: str) -> str:
    return f"storage_quota:{user_id}:{kind}:{sample_key}"


def _quota_row_lock_key(user_id: int, kind: str) -> str:
    """user-kind 级 quota 行锁 key：保护 qrow 首次创建与关键增量更新（相对 sample key 不同）。"""
    return f"storage_quota_row:{user_id}:{kind}"


async def _get_or_create_quota_row_locked(
    session, user_id: int, kind: str, *, used_bytes: int = 0
):
    """在已持 quota-row lock 时取得或创建行。

    SAVEPOINT 只回滚本次 INSERT；唯一键竞争时重查，不回滚调用方外层事务。
    """
    qrow = await _get_quota_row(session, user_id, kind)
    if qrow is not None:
        return qrow
    try:
        async with session.begin_nested():
            qrow = UserStorageQuota(
                user_id=user_id,
                kind=kind,
                used_bytes=_clamp0(used_bytes),
                reserved_bytes=0,
            )
            session.add(qrow)
            flush = getattr(session, "flush", None)
            if flush is not None:
                await flush()
    except IntegrityError:
        qrow = await _get_quota_row(session, user_id, kind)
    if qrow is None:
        qrow = await _get_quota_row(session, user_id, kind)
    if qrow is None:
        raise RuntimeError("quota row creation failed")
    return qrow


async def _with_quota_row_lock(user_id: int, kind: str, operation):
    """用分布式 user-kind GET_LOCK 执行仅修改 quota 行的独立服务调用。"""
    token = await acquire_quota_row_lock(user_id=user_id, kind=kind)
    try:
        if not token.acquired:
            raise QuotaLockUnavailable("quota row lock unavailable")
        return await operation()
    finally:
        await token.release()


def _set_reconcile_state(qrow, *, reason: str, sample_key: Optional[str], marked_at=None):
    """标记待对账；不覆盖另一个样本尚未解决的告警。"""
    current_sample = str(getattr(qrow, "reconcile_sample_key", "") or "")
    new_sample = str(sample_key or "")[:256]
    if bool(getattr(qrow, "reconcile_needed", False)) and current_sample not in ("", new_sample):
        qrow.reconcile_reason = "multiple_samples"
        qrow.reconcile_sample_key = None
        qrow.reconcile_marked_at = marked_at or now_utc()
        return
    qrow.reconcile_needed = True
    qrow.reconcile_reason = str(reason or "")[:128]
    qrow.reconcile_sample_key = new_sample or None
    qrow.reconcile_marked_at = marked_at or now_utc()


def _clear_matching_post_swap_state(qrow, sample_key: Optional[str]):
    if (
        bool(getattr(qrow, "reconcile_needed", False))
        and getattr(qrow, "reconcile_reason", None) == "post_swap_pre_settle"
        and str(getattr(qrow, "reconcile_sample_key", "") or "") == str(sample_key or "")
    ):
        qrow.reconcile_needed = False
        qrow.reconcile_reason = None
        qrow.reconcile_sample_key = None
        qrow.reconcile_marked_at = None


async def mark_reconcile_needed(
    session, *, user_id: int, kind: str, reason: str, sample_key: Optional[str] = None,
    lock_context: Optional[QuotaOperationLock] = None,
) -> None:
    """把 quota 行标记为待对账（文件可能已交换但账面未对齐）。"""
    import logging

    if not is_quota_enabled():
        return
    validate_kind(kind)

    async def _do():
        qrow = await _get_or_create_quota_row_locked(session, user_id, kind)
        _set_reconcile_state(
            qrow, reason=reason, sample_key=sample_key
        )
        try:
            await session.commit()
        except Exception:
            logging.getLogger("quota").error(
                "mark_reconcile_needed failed user=%s kind=%s sample=%s reason=%s",
                user_id, kind, sample_key, reason, exc_info=True,
            )
            raise

    if lock_context is not None:
        if not lock_context.acquired:
            raise QuotaLockUnavailable("quota operation lock unavailable")
        return await _do()
    return await _with_quota_row_lock(user_id, kind, _do)


async def record_usage(
    session, *, user_id: int, kind: str, delta_bytes: int,
    sample_key: Optional[str] = None,
    lock_context: Optional[QuotaOperationLock] = None,
) -> int:
    """dry-run / 管理员豁免路径：仍记录真实用量（观察模式有统计价值）。

    lock_context.acquired=True 时调用方已持组合锁，不再内部加锁。
    """
    if not is_quota_enabled():
        return 0
    validate_kind(kind)

    async def _do():
        qrow = await _get_or_create_quota_row_locked(session, user_id, kind)
        qrow.used_bytes = upsert_delta(int(qrow.used_bytes or 0), int(delta_bytes))
        if not await _has_other_active_reservations(session, user_id, kind):
            _clear_matching_post_swap_state(qrow, sample_key)
        await session.commit()
        return int(qrow.used_bytes or 0)

    if lock_context is not None:
        if not lock_context.acquired:
            raise QuotaLockUnavailable("quota operation lock unavailable")
        return await _do()
    return await _with_quota_row_lock(user_id, kind, _do)


async def settle_quota(
    session,
    *,
    reservation_id: str,
    delta_bytes: int,
    user_id: int,
    kind: str,
    sample_key: str,
    lock_context: Optional[QuotaOperationLock] = None,
) -> int:
    """写盘成功后按真实净差额结算（幂等）。返回结算后 used_bytes。

    lock_context.acquired=True 时调用方已持组合锁，不再内部加锁。
    """
    if not STORAGE_QUOTA_ENABLED:
        return 0
    validate_kind(kind)

    async def _do():
        r = await _get_reservation(session, reservation_id)
        if r is None:
            return 0
        if r.status in ("settled", "released"):
            return _clamp0(r.settled_bytes)
        qrow = await _get_or_create_quota_row_locked(session, user_id, kind)
        new_used = upsert_delta(int(qrow.used_bytes or 0), delta_bytes)
        qrow.used_bytes = new_used
        qrow.reserved_bytes = _clamp0(int(qrow.reserved_bytes or 0) - int(r.requested_bytes or 0))
        r.status = "settled"
        r.settled_bytes = delta_bytes
        r.occupied_bytes = delta_bytes
        r.settled_at = now_utc()
        if not await _has_other_active_reservations(session, user_id, kind, reservation_id):
            _clear_matching_post_swap_state(qrow, sample_key)
        await session.commit()
        return new_used

    if lock_context is not None:
        if not lock_context.acquired:
            raise QuotaLockUnavailable("quota operation lock unavailable")
        return await _do()
    return await _with_quota_row_lock(user_id, kind, _do)


async def release_reservation(
    session, *, reservation_id: str, user_id: int, kind: str, sample_key: str,
    lock_context: Optional[QuotaOperationLock] = None, stored: bool = False,
) -> None:
    """释放预留（幂等）。

    stored=True 表示文件已落盘但未结算，释放后需标记 reconcile_needed。
    lock_context.acquired=True 时调用方已持组合锁，不再内部加锁。
    """
    if not STORAGE_QUOTA_ENABLED:
        return

    async def _do():
        r = await _get_reservation(session, reservation_id)
        if r is None or r.status != "reserved":
            return
        qrow = await _get_or_create_quota_row_locked(session, user_id, kind)
        qrow.reserved_bytes = _clamp0(int(qrow.reserved_bytes or 0) - int(r.requested_bytes or 0))
        if stored:
            _set_reconcile_state(
                qrow, reason="released_after_store", sample_key=sample_key
            )
        r.status = "released"
        r.released_at = now_utc()
        await session.commit()

    if lock_context is not None:
        if not lock_context.acquired:
            raise QuotaLockUnavailable("quota operation lock unavailable")
        return await _do()
    return await _with_quota_row_lock(user_id, kind, _do)


async def reconcile_usage(
    session,
    *,
    user_id: int,
    kind: str,
    storage_label: Optional[str] = None,
    lock_context: Optional[QuotaOperationLock] = None,
) -> int:
    """按真实目录扫描回写 used_bytes（不写 symlink）。成功清 reconcile_needed。

    lock_context.acquired=True 时不内部加锁。
    """
    if not STORAGE_QUOTA_ENABLED:
        return 0
    validate_kind(kind)

    async def _do():
        root = quota_sample_root(kind, storage_label)
        total = logical_usage_bytes(root, kind)
        qrow = await _get_or_create_quota_row_locked(
            session, user_id, kind, used_bytes=total
        )
        qrow.used_bytes = total
        qrow.reconciled_at = now_utc()
        qrow.reconcile_needed = False
        qrow.reconcile_reason = None
        qrow.reconcile_sample_key = None
        qrow.reconcile_marked_at = None
        await session.commit()
        return total

    if lock_context is not None:
        if not lock_context.acquired:
            raise QuotaLockUnavailable("quota operation lock unavailable")
        return await _do()
    return await _with_quota_row_lock(user_id, kind, _do)


async def touch_reservation(
    session,
    *,
    reservation_id: str,
    extend_minutes: int = 30,
    lock_context: Optional[QuotaOperationLock] = None,
) -> bool:
    """延长 active reservation 的过期时间；已有组合锁时不重复取锁。"""
    if not is_quota_enabled() or not reservation_id:
        return False

    async def _do():
        row = await _get_reservation(session, reservation_id)
        if row is None or row.status != "reserved":
            return False
        row.expires_at = now_utc() + timedelta(minutes=max(int(extend_minutes), 1))
        await session.commit()
        return True

    if lock_context is not None:
        if not lock_context.acquired:
            raise QuotaLockUnavailable("quota operation lock unavailable")
        return await _do()
    row = await _get_reservation(session, reservation_id)
    if row is None:
        return False
    token = await acquire_quota_row_lock(user_id=row.user_id, kind=row.kind)
    try:
        if not token.acquired:
            raise QuotaLockUnavailable("quota row lock unavailable")
        return await _do()
    finally:
        await token.release()


@dataclass
class ReclaimOutcome:
    processed: int = 0
    skipped: int = 0
    failed: int = 0


async def reclaim_expired_reservations(
    session, *, now: Optional[datetime] = None
) -> ReclaimOutcome:
    """逐条持 row→sample 锁回收 expires_at 已过期的预留。"""
    outcome = ReclaimOutcome()
    if not is_quota_enabled():
        return outcome
    current = now if now is not None else now_utc()
    result = await session.execute(
        select(StorageQuotaReservation).where(
            StorageQuotaReservation.status == "reserved",
            StorageQuotaReservation.expires_at.isnot(None),
            StorageQuotaReservation.expires_at < current,
        )
    )
    candidates = list(result.scalars().all())
    for candidate in candidates:
        try:
            async with quota_operation_lock(
                candidate.user_id, candidate.kind, candidate.sample_key, timeout=2.0
            ) as op_lock:
                row = await _get_reservation(session, candidate.reservation_id)
                if (
                    row is None
                    or row.status != "reserved"
                    or row.expires_at is None
                    or row.expires_at >= current
                ):
                    outcome.skipped += 1
                    continue
                qrow = await _get_or_create_quota_row_locked(
                    session, row.user_id, row.kind
                )
                qrow.reserved_bytes = _clamp0(
                    int(qrow.reserved_bytes or 0) - int(row.requested_bytes or 0)
                )
                _set_reconcile_state(
                    qrow,
                    reason="reservation_expired",
                    sample_key=row.sample_key,
                    marked_at=current,
                )
                row.status = "expired"
                row.released_at = current
                await session.commit()
                outcome.processed += 1
        except QuotaLockUnavailable:
            outcome.skipped += 1
        except Exception:
            import logging

            logging.getLogger("quota").error(
                "reservation reclaim failed id=%s user=%s kind=%s sample=%s",
                candidate.reservation_id, candidate.user_id, candidate.kind,
                candidate.sample_key, exc_info=True,
            )
            outcome.failed += 1
    return outcome


async def _count_stat(session, user_id, kind, count_field, at_field) -> None:
    if not STORAGE_QUOTA_ENABLED:
        return
    qrow = await _get_or_create_quota_row_locked(session, user_id, kind)
    setattr(qrow, count_field, int(getattr(qrow, count_field, 0) or 0) + 1)
    setattr(qrow, at_field, now_utc())
    await session.commit()


# ---------------------------------------------------------------------------
# 目录 / 清理辅助（清理脚本与 admin 接口复用）
# ---------------------------------------------------------------------------
def quota_sample_root(kind: str, storage_label: Optional[str]) -> Path:
    """按真实目录定位用户样本根（training 语料 / detection 检测库）。"""
    from app.services.paths import _training_corpus_dir_for_label, _user_assets_root_for_label

    validate_kind(kind)
    if not storage_label:
        raise ValueError("storage_label required for quota path")
    if kind == "training":
        return _training_corpus_dir_for_label(storage_label)
    return _user_assets_root_for_label(storage_label) / "detection"


def dir_usage_bytes(path) -> int:
    """递归统计目录真实字节数，不跟随 symlink。"""
    try:
        p = Path(path)
        if not p.is_dir():
            return 0
    except (OSError, TypeError):
        return 0
    total = 0
    for entry in p.rglob("*"):
        if entry.is_symlink():
            continue
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def physical_orphan_stats(root) -> dict:
    """统计配额样本根中的 staging/backup/trash 物理占用，不返回绝对路径。"""
    result = {
        "trash_bytes": 0,
        "staging_bytes": 0,
        "committed_backup_bytes": 0,
        "recovery_backup_bytes": 0,
        "recovery_backup_count": 0,
    }
    try:
        root_path = Path(root)
        entries = list(root_path.iterdir()) if root_path.is_dir() else []
    except OSError:
        return result
    for entry in entries:
        if entry.is_symlink():
            continue
        name = entry.name
        size = dir_usage_bytes(entry) if entry.is_dir() else 0
        if name == ".quota-trash":
            result["trash_bytes"] += size
        elif ".staging-" in name:
            result["staging_bytes"] += size
        elif ".backup-committed-" in name:
            result["committed_backup_bytes"] += size
        elif ".backup-recovery-" in name:
            result["recovery_backup_bytes"] += size
            result["recovery_backup_count"] += 1
    return result


def is_valid_sample_dir(path, kind: str) -> bool:
    """只检查一个目录是否为合法样本，避免清理时每个候选重扫整个根。"""
    validate_kind(kind)
    try:
        entry = Path(path)
        if entry.is_symlink() or not entry.is_dir() or entry.name.startswith("."):
            return False
        name = entry.name
        if ".staging-" in name or ".backup-committed-" in name or ".backup-recovery-" in name:
            return False
        files = [p for p in entry.iterdir() if p.is_file() and not p.is_symlink()]
    except OSError:
        return False
    names = {p.name for p in files}
    if kind == "training":
        return (
            "meta.json" in names
            and any(n.startswith("target.") for n in names)
            and any(n.startswith("result.") for n in names)
        )
    return any(n.startswith("original.") for n in names)


def logical_usage_bytes(root, kind: str) -> int:
    """只统计正式合法样本，不把 trash/staging/backup 算入用户逻辑配额。"""
    return sum(dir_usage_bytes(path) for path in sample_subdir_candidates(root, kind))


def sample_subdir_candidates(root=None, kind=None) -> List[Path]:
    """枚举根目录下可作为样本目录的候选（按真实结构识别）。

    - kind="training"：须含 meta.json 且含 target.* 与 result.*
    - kind="detection"：须含 original.*
    - kind=None：退化为「非隐藏、非 symlink、含内容目录」（兼容旧调用）
    忽略隐藏目录、symlink、普通文件、以及残留 .staging/.backup/.quota-trash。
    """
    try:
        root_p = Path(root)
        if not root_p.is_dir():
            return []
    except (OSError, TypeError):
        return []
    result = []
    for entry in sorted(root_p.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if (
            ".staging-" in entry.name
            or ".backup-committed-" in entry.name
            or ".backup-recovery-" in entry.name
            or entry.name == ".quota-trash"
        ):
            continue
        if kind in STORAGE_QUOTA_KINDS:
            if not is_valid_sample_dir(entry, kind):
                continue
        else:
            try:
                if not any(True for _ in entry.iterdir()):
                    continue
            except OSError:
                continue
        result.append(entry)
    return result


def latest_mtime(dir_path) -> Optional[float]:
    """目录内所有文件（含子目录）的最大 mtime；不跟随 symlink。空返回 None。"""
    latest = None
    try:
        for entry in Path(dir_path).rglob("*"):
            if entry.is_symlink():
                continue
            try:
                if entry.is_file():
                    m = entry.stat().st_mtime
                    if latest is None or m > latest:
                        latest = m
            except OSError:
                continue
    except OSError:
        return latest
    return latest


def expired_sample_dirs(root, retention_days: int, now: Optional[float] = None) -> List[Path]:
    """返回「目录内最大文件 mtime」超过 retention 的样本目录列表。

    retention_days<=0 时返回空（绝对不删除）。"""
    if retention_days <= 0:
        return []
    now = now if now is not None else time.time()
    cutoff = now - retention_days * 86400
    expired = []
    for cand in sample_subdir_candidates(root):
        mtime = latest_mtime(cand)
        if mtime is not None and mtime < cutoff:
            expired.append(cand)
    return expired