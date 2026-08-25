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
from typing import Dict, List, Optional

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
# 命名锁（进程内；分布式部署下与 MySQL GET_LOCK 同一 key 语义对齐）
# ---------------------------------------------------------------------------
_LOCKS: Dict[str, object] = {}


def _lock_for(user_id: int, kind: str, sample_key: str):
    import asyncio

    key = f"{user_id}:{kind}:{sample_key}"
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


# ---------------------------------------------------------------------------
# 输出对象与纯逻辑辅助
# ---------------------------------------------------------------------------
@dataclass
class ReserveOutcome:
    allowed: bool
    reservation_id: Optional[str] = None
    would_deny: bool = False
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


async def _get_user_role(session, user_id: int) -> Optional[str]:
    stmt = select(User.role).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _mysql_lock(session, key: str, timeout: float = 5.0) -> bool:
    try:
        result = await session.execute(text("SELECT GET_LOCK(:k, :t)"), {"k": key, "t": timeout})
        return bool(result.scalar())
    except Exception:
        return False


async def _mysql_unlock(session, key: str) -> None:
    try:
        await session.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": key})
    except Exception:
        pass


# 老名兼容
_mysql_release = _mysql_unlock


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


async def acquire_transaction_lock(
    session, *, user_id: int, kind: str, sample_key: str, timeout: float = 5.0
) -> QuotaLockToken:
    """兼容旧名：委托给 acquire_sample_lock（独立连接版）。"""
    return await acquire_sample_lock(user_id=user_id, kind=kind, sample_key=sample_key, timeout=timeout)


def _new_reservation_id() -> str:
    return _uuid.uuid4().hex


# ---------------------------------------------------------------------------
# 核心对外 API
# ---------------------------------------------------------------------------
async def reserve_quota(
    session,
    *,
    user_id: int,
    role: Optional[str],
    storage_label: Optional[str] = None,
    kind: str,
    sample_key: str,
    incoming_bytes: int,
    lock_token: Optional[QuotaLockToken] = None,
) -> ReserveOutcome:
    """上传前一次性配额预留（QUOTA_ENABLED=0 时立即返回 allowed，零 DB 写）。

    路由先通过 acquire_sample_lock 拿到独立连接上的 GET_LOCK 令牌并传入本函数，
    本函数不再重复 GET_LOCK。dry-run 不创建假的 reservation_id。
    管理员豁免：只标记 admin_exempt，实际用量由写盘成功后 record_usage(actual_delta) 记录。
    """
    validate_kind(kind)
    if not is_quota_enabled():
        return ReserveOutcome(allowed=True, reason="disabled")

    incoming_bytes = _clamp0(incoming_bytes)
    dry_run = is_dry_run()

    try:
        db_role = await _get_user_role(session, user_id)
    except Exception:
        db_role = role
    if is_admin_role(db_role):
        return ReserveOutcome(allowed=True, reason="admin_exempt")

    try:
        qrow = await _get_quota_row(session, user_id, kind)
        quota_bytes = default_quota_bytes(kind)
        if qrow is not None and qrow.quota_mb is not None:
            quota_bytes = _clamp0(qrow.quota_mb) * 1024 * 1024
        used = _clamp0(getattr(qrow, "used_bytes", 0))
        reserved = _clamp0(getattr(qrow, "reserved_bytes", 0))

        has_lock = bool(lock_token) and bool(lock_token.acquired)
        needs_reconcile = qrow is None or getattr(qrow, "reconciled_at", None) is None
        if not dry_run and (not has_lock or needs_reconcile):
            reason = "reconcile_required" if needs_reconcile else "quota_unavailable"
            return ReserveOutcome(
                allowed=False,
                reason=reason,
                used_bytes=used,
                quota_bytes=quota_bytes,
            )

        exceed = would_exceed(used, reserved, incoming_bytes, quota_bytes)
        if not exceed:
            if dry_run:
                return ReserveOutcome(allowed=True, used_bytes=used, quota_bytes=quota_bytes)

            reservation_id = _new_reservation_id()
            if qrow is None:
                # 首次并发创建（不同 sample_key 同 user+kind）可能唯一键冲突：
                # 这里创建，冲突 rollback+重查一次，避免唯一键竞态。
                try:
                    qrow = UserStorageQuota(
                        user_id=user_id, kind=kind, used_bytes=0, reserved_bytes=0
                    )
                    session.add(qrow)
                except IntegrityError:
                    await session.rollback()
                    qrow = await _get_quota_row(session, user_id, kind)
            qrow.reserved_bytes = _clamp0(qrow.reserved_bytes + incoming_bytes)
            session.add(
                StorageQuotaReservation(
                    reservation_id=reservation_id,
                    user_id=user_id,
                    kind=kind,
                    sample_key=sample_key,
                    status="reserved",
                    requested_bytes=incoming_bytes,
                    occupied_bytes=0,
                    settled_bytes=0,
                    expires_at=now_utc() + timedelta(minutes=30),
                )
            )
            await session.commit()
            return ReserveOutcome(
                allowed=True,
                reservation_id=reservation_id,
                used_bytes=used,
                quota_bytes=quota_bytes,
            )

        if dry_run:
            await _count_stat(session, user_id, kind, "would_deny_count", "last_would_deny_at")
            return ReserveOutcome(
                allowed=True, would_deny=True, used_bytes=used, quota_bytes=quota_bytes,
            )

        await _count_stat(session, user_id, kind, "denied_count", "last_denied_at")
        return ReserveOutcome(
            allowed=False, used_bytes=used, quota_bytes=quota_bytes, reason="quota_exceeded",
        )
    except Exception:
        if dry_run:
            return ReserveOutcome(
                allowed=True, dry_run_fail_open=True, used_bytes=0,
                quota_bytes=default_quota_bytes(kind),
            )
        raise


def _lock_key(user_id: int, kind: str, sample_key: str) -> str:
    return f"storage_quota:{user_id}:{kind}:{sample_key}"


async def record_usage(
    session, *, user_id: int, kind: str, delta_bytes: int
) -> int:
    """dry-run / 管理员豁免路径：仍记录真实用量（观察模式有统计价值）。

    delta_bytes 可负（覆盖变小）。used 下限 0。返回结算后 used_bytes。
    开关关闭时 no-op。
    """
    if not is_quota_enabled():
        return 0
    validate_kind(kind)
    qrow = await _get_quota_row(session, user_id, kind)
    if qrow is None:
        qrow = UserStorageQuota(user_id=user_id, kind=kind, used_bytes=0, reserved_bytes=0)
        session.add(qrow)
    qrow.used_bytes = upsert_delta(int(qrow.used_bytes or 0), int(delta_bytes))
    await session.commit()
    return int(qrow.used_bytes or 0)


async def settle_quota(
    session,
    *,
    reservation_id: str,
    delta_bytes: int,
    user_id: int,
    kind: str,
    sample_key: str,
) -> int:
    """写盘成功后按真实净差额结算（幂等）。返回结算后 used_bytes。

    净差额可为 0 或负（覆盖变小）；调用方须在白名单未被拒绝时调用，释放预留。
    """
    if not STORAGE_QUOTA_ENABLED:
        return 0
    validate_kind(kind)
    async with _lock(user_id, kind, sample_key):
        r = await _get_reservation(session, reservation_id)
        if r is None:
            return 0
        if r.status in ("settled", "released"):
            return _clamp0(r.settled_bytes)
        qrow = await _get_quota_row(session, user_id, kind)
        if qrow is None:
            qrow = UserStorageQuota(user_id=user_id, kind=kind, used_bytes=0, reserved_bytes=0)
            session.add(qrow)
        new_used = upsert_delta(int(qrow.used_bytes or 0), delta_bytes)
        qrow.used_bytes = new_used
        qrow.reserved_bytes = _clamp0(int(qrow.reserved_bytes or 0) - int(r.requested_bytes or 0))
        r.status = "settled"
        r.settled_bytes = delta_bytes
        r.occupied_bytes = delta_bytes
        r.settled_at = now_utc()
        qrow.reconcile_needed = False
        await session.commit()
        return new_used


async def release_reservation(
    session, *, reservation_id: str, user_id: int, kind: str, sample_key: str
) -> None:
    """写盘失败时释放预留（幂等）。"""
    if not STORAGE_QUOTA_ENABLED:
        return
    async with _lock(user_id, kind, sample_key):
        r = await _get_reservation(session, reservation_id)
        if r is None or r.status != "reserved":
            return
        qrow = await _get_quota_row(session, user_id, kind)
        if qrow is None:
            qrow = UserStorageQuota(user_id=user_id, kind=kind, used_bytes=0, reserved_bytes=0)
            session.add(qrow)
        qrow.reserved_bytes = _clamp0(int(qrow.reserved_bytes or 0) - int(r.requested_bytes or 0))
        r.status = "released"
        r.released_at = now_utc()
        await session.commit()


async def reconcile_usage(
    session,
    *,
    user_id: int,
    kind: str,
    storage_label: Optional[str] = None,
) -> int:
    """按真实目录扫描回写 used_bytes（不跟随 symlink）。返回实际用量。"""
    if not STORAGE_QUOTA_ENABLED:
        return 0
    validate_kind(kind)
    root = quota_sample_root(kind, storage_label)
    total = dir_usage_bytes(root)
    qrow = await _get_quota_row(session, user_id, kind)
    if qrow is None:
        qrow = UserStorageQuota(user_id=user_id, kind=kind, used_bytes=total, reserved_bytes=0)
        session.add(qrow)
    qrow.used_bytes = total
    qrow.reconciled_at = now_utc()
    await session.commit()
    return total


async def reclaim_expired_reservations(
    session, *, cutoff: Optional[datetime] = None, now: Optional[datetime] = None
) -> int:
    """回收超过 cutoff 仍未 settle/release 的预留。返回回收数量。"""
    if not STORAGE_QUOTA_ENABLED:
        return 0
    now = now if now is not None else now_utc()
    result = await session.execute(
        select(StorageQuotaReservation).where(
            StorageQuotaReservation.status == "reserved",
            StorageQuotaReservation.expires_at.isnot_(None),
            StorageQuotaReservation.expires_at < now,
        )
    )
    rows = list(result.scalars().all())
    count = 0
    for row in rows:
        r = await _get_reservation(session, row.reservation_id)
        if r is None or r.status != "reserved":
            continue
        if r.expires_at is None or r.expires_at >= now:
            continue
        qrow = await _get_quota_row(session, r.user_id, r.kind)
        if qrow is not None:
            qrow.reserved_bytes = _clamp0(int(qrow.reserved_bytes or 0) - int(r.requested_bytes or 0))
            qrow.reconcile_needed = True
        r.status = "expired"
        r.released_at = now
        await session.commit()
        count += 1
    return count


async def _count_stat(session, user_id, kind, count_field, at_field) -> None:
    if not STORAGE_QUOTA_ENABLED:
        return
    qrow = await _get_quota_row(session, user_id, kind)
    if qrow is None:
        qrow = UserStorageQuota(user_id=user_id, kind=kind, used_bytes=0, reserved_bytes=0)
        session.add(qrow)
    setattr(qrow, count_field, int(getattr(qrow, count_field, 0) or 0) + 1)
    setattr(qrow, at_field, now_utc())
    await session.commit()


def _lock(user_id, kind, sample_key):
    return _lock_for(user_id, kind, sample_key)


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
        if entry.name.endswith((".staging", ".backup", ".quota-trash")):
            continue
        try:
            names = {p.name for p in entry.iterdir()}
        except OSError:
            continue

        def _has(prefix):
            return any(n.startswith(prefix) for n in names if not n.endswith((".staging", ".backup")))

        if kind == "training":
            if "meta.json" not in names:
                continue
            if not (_has("target") and _has("result")):
                continue
        elif kind == "detection":
            if not _has("original"):
                continue
        else:
            if not names:
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