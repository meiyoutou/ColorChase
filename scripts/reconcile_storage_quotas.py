#!/usr/bin/env python3
"""批量初始化/修正用户训练与检测配额用量（不删除任何文件）。

仅在 COLORCHASE_USER_STORAGE_QUOTA_ENABLED=1 时运行；逐 user/kind 通过
reconcile_usage 获取 quota-row lock，扫描正式样本并回写 used_bytes/reconciled_at。
建议启用 dry-run 观测模式后手工执行一次，再考虑强制模式。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.services import storage_quota as sq
from app.settings import STORAGE_QUOTA_ENABLED
from database import async_session
from models import User


async def main():
    if not STORAGE_QUOTA_ENABLED:
        print("Storage quota disabled; reconciliation not run.")
        return
    stats = {"processed": 0, "skipped": 0, "failed": 0}
    async with async_session() as session:
        users = (await session.execute(select(User))).scalars().all()
        for user in users:
            label = str(getattr(user, "storage_label", "") or "").strip()
            if not label:
                stats["skipped"] += 1
                continue
            for kind in sq.STORAGE_QUOTA_KINDS:
                try:
                    used = await sq.reconcile_usage(
                        session,
                        user_id=int(user.id),
                        kind=kind,
                        storage_label=label,
                    )
                    stats["processed"] += 1
                    print(f"reconciled user={user.id} kind={kind} used_bytes={used}")
                except Exception as exc:
                    stats["failed"] += 1
                    print(f"[failed] user={user.id} kind={kind}: {exc}")
    print(f"reconciliation finished. {stats}")


if __name__ == "__main__":
    asyncio.run(main())
