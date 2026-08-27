#!/usr/bin/env python3
"""定期清理过期训练/检测数据（独立脚本，默认关闭）。

只有 RETENTION_DAYS>0 时执行；dry-run 只报告。正式样本先原子移入
.quota-trash，再立即对账逻辑用量，最后尝试物理删除。recovery backup 永不
自动删除；staging/committed backup/trash 子项仅在超过安全年龄后回收。
"""
import asyncio
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.services import storage_quota as sq
from database import async_session
from models import StorageQuotaReservation, User

RESIDUAL_MIN_AGE = 6 * 3600
TRASH_DIR_NAME = ".quota-trash"
RESIDUAL_RE = re.compile(
    r"^[A-Za-z0-9_.-]+\.(?:staging|backup-committed)-[a-f0-9]{8}$"
)


def _cand_expired(cand: Path, retention: int) -> bool:
    if retention <= 0:
        return False
    mtime = sq.latest_mtime(cand)
    return mtime is not None and mtime < time.time() - retention * 86400


async def _has_active_reservation(session, *, user_id: int, kind: str, sample_key: str) -> bool:
    result = await session.execute(
        select(StorageQuotaReservation.id).where(
            StorageQuotaReservation.user_id == user_id,
            StorageQuotaReservation.kind == kind,
            StorageQuotaReservation.sample_key == sample_key,
            StorageQuotaReservation.status == "reserved",
        )
    )
    return result.first() is not None


async def _trash_and_delete(
    session, *, uid: int, label: str, root: Path, cand: Path,
    kind: str, dry_run: bool, stats: dict, lock_context,
) -> None:
    trash_dir = root / TRASH_DIR_NAME
    if dry_run:
        stats["would_delete"] += 1
        print(f"[DRY_RUN] would trash: {cand.name}")
        return
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / f"{cand.name}-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(cand, dest)
    except OSError as exc:
        stats["failed"] += 1
        print(f"[failed] rename {cand}: {exc}")
        return
    stats["trashed"] += 1

    # 正式样本已移出逻辑区；无论后续物理删除是否成功都立即对账。
    try:
        await sq.reconcile_usage(
            session, user_id=uid, kind=kind, storage_label=label,
            lock_context=lock_context,
        )
    except Exception as exc:
        stats["reconcile_failed"] += 1
        print(f"[error] reconcile failed user={uid} kind={kind}: {exc}")

    try:
        shutil.rmtree(dest)
        stats["deleted"] += 1
    except OSError as exc:
        stats["failed"] += 1
        print(f"[failed] rmtree {dest}: {exc} (left for retry)")


async def _cleanup_kind(
    session, *, uid: int, label: str, root: Path, kind: str,
    retention: int, dry_run: bool, stats: dict,
) -> None:
    for cand in sq.sample_subdir_candidates(root, kind):
        try:
            async with sq.quota_operation_lock(uid, kind, cand.name, timeout=2.0) as op_lock:
                if not sq.is_valid_sample_dir(cand, kind) or not _cand_expired(cand, retention):
                    stats["skip_fresh"] += 1
                    continue
                if await _has_active_reservation(
                    session, user_id=uid, kind=kind, sample_key=cand.name
                ):
                    stats["skip_active"] += 1
                    continue
                await _trash_and_delete(
                    session, uid=uid, label=label, root=root, cand=cand,
                    kind=kind, dry_run=dry_run, stats=stats,
                    lock_context=op_lock,
                )
        except sq.QuotaLockUnavailable:
            stats["skip_lock"] += 1
        except Exception as exc:
            stats["failed"] += 1
            print(f"[error] {cand}: {exc}")


def _residual_targets(root: Path):
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    targets = []
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.name == TRASH_DIR_NAME and entry.is_dir():
            try:
                targets.extend(
                    sub for sub in entry.iterdir()
                    if not sub.is_symlink() and not sub.name.startswith(".")
                )
            except OSError:
                pass
        elif RESIDUAL_RE.match(entry.name):
            targets.append(entry)
        # backup-recovery 故意不匹配：可能是唯一恢复副本，永不自动删除。
    return targets


def _cleanup_residuals(root: Path, dry_run: bool, now: float, stats: dict) -> None:
    for entry in _residual_targets(root):
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < RESIDUAL_MIN_AGE:
            continue
        if dry_run:
            stats["would_delete"] += 1
            print(f"[DRY_RUN] would purge residual: {entry.name}")
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            stats["deleted"] += 1
        except OSError as exc:
            stats["failed"] += 1
            print(f"[failed] purge residual {entry}: {exc}")


async def main():
    from app.settings import (
        STORAGE_QUOTA_DRY_RUN,
        STORAGE_QUOTA_ENABLED,
        TRAINING_RETENTION_DAYS,
    )

    if not STORAGE_QUOTA_ENABLED:
        print("Storage quota disabled; cleanup disabled, nothing deleted.")
        return
    retention = int(TRAINING_RETENTION_DAYS or 0)
    if retention <= 0:
        print("Retention days <= 0; cleanup disabled, nothing deleted.")
        return
    dry_run = bool(STORAGE_QUOTA_DRY_RUN)
    stats = {
        "trashed": 0, "deleted": 0, "failed": 0, "would_delete": 0,
        "skip_lock": 0, "skip_active": 0, "skip_fresh": 0,
        "reconcile_failed": 0,
    }
    print(f"cleanup start: retention={retention}d dry_run={dry_run}")
    async with async_session() as session:
        if not dry_run:
            reclaim = await sq.reclaim_expired_reservations(session)
            print(
                "reservation reclaim: "
                f"processed={reclaim.processed} skipped={reclaim.skipped} failed={reclaim.failed}"
            )
        users = (await session.execute(select(User))).scalars().all()
        for user in users:
            label = str(getattr(user, "storage_label", "") or "")
            if not label:
                continue
            uid = int(getattr(user, "id", 0) or 0)
            for kind in ("training", "detection"):
                try:
                    root = sq.quota_sample_root(kind, label)
                except Exception:
                    continue
                if not root.is_dir():
                    continue
                await _cleanup_kind(
                    session, uid=uid, label=label, root=root, kind=kind,
                    retention=retention, dry_run=dry_run, stats=stats,
                )
                _cleanup_residuals(root, dry_run, time.time(), stats)
        print(f"cleanup finished. {stats}")


if __name__ == "__main__":
    asyncio.run(main())
