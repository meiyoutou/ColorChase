#!/usr/bin/env python3
"""定期清理过期训练/检测数据（独立脚本，默认全关）。

- RETENTION_DAYS<=0（默认）立即退出，绝对不删除任何数据。
- dry-run（默认 QUOTA_DRY_RUN=1）只报告不删除。
- 以「样本目录」为单位删除（整目录），不逐文件删除。
- 判定过期 = 目录内最大文件 mtime 超过保留天数。
- 忽略隐藏目录、symlink、不符合样本结构的目录。
- 检测库按 UUID 子目录递归处理。
- 清理完成后对受影响用户调用 reconcile_usage 回写真实用量。
- 需外部定时（crontab / systemd timer）调度，不挂应用 lifespan。
"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import storage_quota as sq
from database import async_session
from models import User
from sqlalchemy import select  # noqa: E402


async def main():
    from app.settings import STORAGE_QUOTA_DRY_RUN, TRAINING_RETENTION_DAYS

    retention = int(TRAINING_RETENTION_DAYS or 0)
    if retention <= 0:
        print("Retention days <= 0; cleanup disabled, nothing deleted.")
        return
    dry_run = bool(STORAGE_QUOTA_DRY_RUN)
    print(f"cleanup start: retention={retention}d dry_run={dry_run}")

    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        total = 0
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
                candidates = sq.expired_sample_dirs(root, retention)
                n = 0
                for cand in candidates:
                    if dry_run:
                        print(f"[DRY_RUN] would delete: {cand}")
                    else:
                        try:
                            shutil.rmtree(cand, ignore_errors=True)
                            print(f"deleted: {cand}")
                            n += 1
                        except Exception as exc:
                            print(f"delete failed {cand}: {exc}")
                if n and not dry_run:
                    try:
                        await sq.reconcile_usage(
                            session, user_id=uid, kind=kind, storage_label=label
                        )
                    except Exception:
                        pass
                total += n
        print(f"cleanup finished. affected dirs: {total}")


if __name__ == "__main__":
    asyncio.run(main())