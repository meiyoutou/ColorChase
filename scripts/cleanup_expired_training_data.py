#!/usr/bin/env python3
"""定期清理过期训练/检测数据（独立脚本，默认全关）。

- RETENTION_DAYS<=0（默认）立即退出，绝对不删除任何数据。
- dry-run（默认 QUOTA_DRY_RUN=1）只报告，绝不 rename/rmtree/改 used_bytes。
- 以「样本目录」为单位：先原子 rename 到 root/.quota-trash/，成功才删除。
- 判定过期 = 目录内最大文件 mtime 超过保留天数。
- 忽略隐藏目录、symlink、普通文件、残留 .staging/.backup/.quota-trash。

流程：
  1) reclaim_expired_reservations 回收过期预留
  2) 逐用户/kind：
     - sample_subdir_candidates(root, kind) 结构识别
     - 对每个候选取与上传相同的 sample lock；失败跳过
     - 检查 active reservation，有则跳过
     - 二次检查过期状态（拿锁后若变新则跳过）
     - rename 到 root/.quota-trash/<unique>；成功才计入 affected
     - rmtree 失败计入 failed，trash 留待下次重试（不宣称已删）
  3) 清理残留 .staging/.backup/.quota-trash（须超出最小年龄，不碰当天活动文件）
  4) 影响真实变化后才 reconcile_usage
"""
import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import storage_quota as sq
from database import async_session
from models import StorageQuotaReservation, User
from sqlalchemy import select  # noqa: E402

RESIDUAL_MIN_AGE = 6 * 3600  # 残留文件须 >6h 才清理，不碰当天活动文件
TRASH_DIR_NAME = ".quota-trash"


def _cand_expired(cand: Path, retention: int) -> bool:
    """判断单个样本目录是否过期（目录内最大文件 mtime 超过保留期）。"""
    if retention <= 0:
        return False
    mtime = sq.latest_mtime(cand)
    if mtime is None:
        return False
    return mtime < (time.time() - retention * 86400)


async def _has_active_reservation(session, *, user_id: int, kind: str, sample_key: str) -> bool:
    """查询 DB：该 user/kind/sample 是否有 status='reserved' 的预留存在。"""
    result = await session.execute(
        select(StorageQuotaReservation.id).where(
            StorageQuotaReservation.user_id == user_id,
            StorageQuotaReservation.kind == kind,
            StorageQuotaReservation.sample_key == sample_key,
            StorageQuotaReservation.status == "reserved",
        )
    )
    return result.first() is not None


async def _trash_and_delete(root: Path, cand: Path, dry_run: bool, stats: dict) -> None:
    """把过期样本 rename 到 trash 再删除；rename 成功才计入 affected。"""
    trash_dir = root / TRASH_DIR_NAME
    if dry_run:
        print(f"[DRY_RUN] would trash: {cand} -> {trash_dir}")
        stats["would_delete"] += 1
        return
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / f"{cand.name}-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(str(cand), str(dest))
        stats["affected"] += 1
        print(f"trashed: {cand} -> {dest}")
        try:
            shutil.rmtree(dest, ignore_errors=False)
        except Exception as exc:
            stats["failed"] += 1
            print(f"[failed] rmtree {dest}: {exc} (trash left for retry)")
    except OSError as exc:
        stats["failed"] += 1
        print(f"[failed] rename {cand}: {exc}")


async def _cleanup_kind(session, *, uid: int, root, kind: str, retention: int, dry_run: bool, stats: dict) -> None:
    """清理单用户单 kind 下的过期样本。uid 必须是该样本拥有者的真实 user_id。"""
    for cand in sq.sample_subdir_candidates(root, kind):
        locked = False
        token = None
        try:
            token = await sq.acquire_sample_lock(user_id=uid, kind=kind, sample_key=cand.name, timeout=2.0)
            locked = token.acquired
            if not locked:
                stats["skip_lock"] += 1
                print(f"[skip] lock failed: {cand}")
                continue
            # 拿锁后二次检查过期状态（拿锁前后可能变化）——判定当前 cand 是否仍过期
            if not _cand_expired(cand, retention):
                stats["skip_fresh"] += 1
                print(f"[skip] sample not expired: {cand}")
                continue
            if cand not in sq.sample_subdir_candidates(root, kind):
                stats["skip_fresh"] += 1
                print(f"[skip] sample no longer valid: {cand}")
                continue
            if await _has_active_reservation(session, user_id=uid, kind=kind, sample_key=cand.name):
                stats["skip_active"] += 1
                print(f"[skip] active reservation: {cand}")
                continue
            await _trash_and_delete(root, cand, dry_run, stats)
        except Exception as exc:
            stats["failed"] += 1
            print(f"[error] {cand}: {exc}")
        finally:
            if locked and token is not None:
                await token.release()


def _cleanup_residuals(root: Path, dry_run: bool, now: float) -> None:
    """清理残留 staging/backup/trash：只匹配本功能固定命名。

    命名格式：<sample>.staging-<8hex> / <sample>.backup-<8hex> / 根下 .quota-trash。
    仅列根目录（不递归遍历删除父后再继续子项），超最小年龄才清理。
    """
    import re as _re

    resid_re = _re.compile(r"^(?P<sample>[A-Za-z0-9_.-]+)\.(?:staging|backup)-(?P<rid>[a-f0-9]{8})$")
    targets = []
    try:
        for entry in root.iterdir():
            if entry.is_symlink():
                continue
            if entry.name == TRASH_DIR_NAME and entry.is_dir():
                # .quota-trash：只枚举其下具体子目录逐个清计数，不删整个父目录
                try:
                    for sub in entry.iterdir():
                        if sub.is_symlink() or sub.name.startswith("."):
                            continue
                        targets.append(sub)
                except OSError:
                    pass
                continue
            m = resid_re.match(entry.name)
            if m:
                targets.append(entry)
    except OSError:
        return
    for entry in targets:
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < RESIDUAL_MIN_AGE:
            continue
        if dry_run:
            print(f"[DRY_RUN] would purge residual: {entry}")
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            print(f"[failed] purge residual: {entry}")


async def main():
    from app.settings import STORAGE_QUOTA_DRY_RUN, TRAINING_RETENTION_DAYS

    retention = int(TRAINING_RETENTION_DAYS or 0)
    if retention <= 0:
        print("Retention days <= 0; cleanup disabled, nothing deleted.")
        return
    dry_run = bool(STORAGE_QUOTA_DRY_RUN)
    print(f"cleanup start: retention={retention}d dry_run={dry_run}")

    stats = {"affected": 0, "failed": 0, "skip_lock": 0, "skip_active": 0, "skip_fresh": 0}
    async with async_session() as session:
        if not dry_run:
            await sq.reclaim_expired_reservations(session)
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
                if not root.exists() or not root.is_dir():
                    continue
                before = stats["affected"]
                await _cleanup_kind(session, uid=uid, root=root, kind=kind,
                                    retention=retention, dry_run=dry_run, stats=stats)
                _cleanup_residuals(root, dry_run, time.time())
                if stats["affected"] != before and not dry_run:
                    try:
                        await sq.reconcile_usage(session, user_id=uid, kind=kind, storage_label=label)
                    except Exception as exc:
                        stats["reconcile_failed"] = stats.get("reconcile_failed", 0) + 1
                        print(f"[error] reconcile failed user={uid} kind={kind}: {exc} (reconcile_needed)")
        print(f"cleanup finished. {stats}")


if __name__ == "__main__":
    asyncio.run(main())