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
from models import StorageQuotaReservation, User, UserStorageQuota


def now_utc() -> datetime:
    return datetime.utcnow()


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
) -> ReserveOutcome:
    """上传前调用。QUOTA_ENABLED=0 时立即返回 allowed（零 DB 写）。"""
    validate_kind(kind)
    if not STORAGE_QUOTA_ENABLED:
        return ReserveOutcome(allowed=True, reason="disabled")

    incoming_bytes = _clamp0(incoming_bytes)
    dry_run = STORAGE_QUOTA_DRY_RUN
    lock_key = f"storage_quota:{user_id}:{kind}:{sample_key}"

    # 管理员豁免：以 DB User.role + is_admin_role 为准（入参 role 仅作提示）
    try:
        db_role = await _get_user_role(session, user_id)
    except Exception:
        db_role = role
    if is_admin_role(db_role):
        return ReserveOutcome(allowed=True, reason="admin_exempt")

    async with _lock(user_id, kind, sample_key):
        got = await _mysql_lock(session, lock_key)
        try:
            qrow = await _get_quota_row(session, user_id, kind)
            quota_bytes = default_quota_bytes(kind)
            if qrow is not None and qrow.quota_mb is not None:
                quota_bytes = _clamp0(qrow.quota_mb) * 1024 * 1024
            used = _clamp0(getattr(qrow, "used_bytes", 0))
            reserved = _clamp0(getattr(qrow, "reserved_bytes", 0))

            exceed = would_exceed(used, reserved, incoming_bytes, quota_bytes)
            if not exceed:
                reservation_id = _new_reservation_id()
                if not dry_run:
                    if qrow is None:
                        qrow = UserStorageQuota(user_id=user_id, kind=kind, used_bytes=0, reserved_bytes=0)
                        session.add(qrow)
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
                    allowed=True,
                    would_deny=True,
                    used_bytes=used,
                    quota_bytes=quota_bytes,
                )

            await _count_stat(session, user_id, kind, "denied_count", "last_denied_at")
            return ReserveOutcome(
                allowed=False,
                used_bytes=used,
                quota_bytes=quota_bytes,
                reason="quota_exceeded",
            )
        finally:
            if got:
                await _mysql_unlock(session, lock_key)


async def settle_quota(
    session,
    *,
    reservation_id: str,
    delta_bytes: int,
    user_id: int,
    kind: str,
    sample_key: str,
) -> int:
    """写盘成功后按真实净差额结算（幂等）。返回结算后 used_bytes。"""
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
    cutoff = cutoff or (now - timedelta(minutes=30))
    result = await session.execute(
        select(StorageQuotaReservation).where(
            StorageQuotaReservation.status == "reserved",
            StorageQuotaReservation.created_at < cutoff,
        )
    )
    rows = list(result.scalars().all())
    count = 0
    for row in rows:
        r = await _get_reservation(session, row.reservation_id)
        if r is None or r.status != "reserved":
            continue
        qrow = await _get_quota_row(session, r.user_id, r.kind)
        if qrow is not None:
            qrow.reserved_bytes = _clamp0(int(qrow.reserved_bytes or 0) - int(r.requested_bytes or 0))
        r.status = "released"
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


def sample_subdir_candidates(root) -> List[Path]:
    """枚举根目录下可作为样本目录的候选（按目录结构识别，不要求标准 UUID 命名）。

    规则：忽略隐藏目录、symlink、非目录；任何包含文件或子目录的非隐藏目录视为样本单元。
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
        try:
            has_content = any(True for _ in entry.iterdir())
        except OSError:
            continue
        if has_content:
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