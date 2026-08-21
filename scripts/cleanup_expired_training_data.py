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
from models import User
from sqlalchemy import select  # noqa: E402

RESIDUAL_MIN_AGE = 6 * 3600  # 残留文件须 >6h 才清理，不碰当天活动文件
TRASH_DIR_NAME = ".quota-trash"


def _sync_has_active_reservation(sample_dir: Path) -> bool:
    """目录内存在 .active_reservation 标记即视为有活动预留（DB 一致信号，供注入）。"""
    return (sample_dir / ".active_reservation").exists()


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


async def _cleanup_kind(session, root: Path, kind: str, retention: int, dry_run: bool, stats: dict) -> None:
    """清理单用户单 kind 下的过期样本。"""
    for cand in sq.sample_subdir_candidates(root, kind):
        locked = False
        token = None
        try:
            token = await sq.acquire_sample_lock(user_id=0, kind=kind, sample_key=cand.name, timeout=2.0)
            locked = token.acquired
            if not locked:
                stats["skip_lock"] += 1
                print(f"[skip] lock failed: {cand}")
                continue
            if _sync_has_active_reservation(cand):
                stats["skip_active"] += 1
                print(f"[skip] active reservation: {cand}")
                continue
            # 拿锁后二次检查过期状态（拿锁前后可能变化）
            if cand not in sq.sample_subdir_candidates(root, kind):
                stats["skip_fresh"] += 1
                print(f"[skip] sample no longer valid: {cand}")
                continue
            if not sq.expired_sample_dirs(root, retention):
                stats["skip_fresh"] += 1
                print(f"[skip] sample became fresh: {cand}")
                continue
            await _trash_and_delete(root, cand, dry_run, stats)
        except Exception as exc:
            print(f"[error] {cand}: {exc}")
        finally:
            if locked and token is not None:
                await token.release()


def _cleanup_residuals(root: Path, dry_run: bool, now: float) -> None:
    """清理残留 .staging/.backup/.quota-trash：仅处理超过最小年龄的。"""
    for entry in root.rglob("*"):
        if entry.is_symlink():
            continue
        is_residual = (
            entry.name.endswith((".staging", ".backup"))
            or (entry.is_dir() and entry.name == TRASH_DIR_NAME)
        )
        if not is_residual:
            continue
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
                shutil.rmtree(entry, ignore_errors=True)
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
                await _cleanup_kind(session, root, kind, retention, dry_run, stats)
                _cleanup_residuals(root, dry_run, time.time())
                if stats["affected"] != before and not dry_run:
                    try:
                        await sq.reconcile_usage(session, user_id=uid, kind=kind, storage_label=label)
                    except Exception:
                        pass
        print(f"cleanup finished. {stats}")


if __name__ == "__main__":
    asyncio.run(main())