"""真实 MySQL 存储配额集成测试。

仅当 COLORCHASE_TEST_DATABASE_URL 指向数据库名包含 ``test`` 的独立测试库时执行。
没有 URL 时整文件 skip；本文件不得连接生产数据库，也不 drop 共享表。
"""
import asyncio
import os
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import storage_quota as sq
from database import Base
from models import StorageQuotaReservation, User, UserStorageQuota

RAW_DB_URL = os.environ.get("COLORCHASE_TEST_DATABASE_URL", "").strip()
DB_URL = (
    "mysql+aiomysql://" + RAW_DB_URL.split("://", 1)[1]
    if RAW_DB_URL.startswith("mysql+pymysql://") else RAW_DB_URL
)
DB_NAME = urlsplit(DB_URL.replace("mysql+aiomysql", "mysql")).path.lstrip("/") if DB_URL else ""
DB_NAME_LOWER = DB_NAME.lower()
SAFE_TEST_DB = bool(
    DB_URL
    and (
        DB_NAME_LOWER == "test"
        or DB_NAME_LOWER.startswith("test_")
        or DB_NAME_LOWER.endswith("_test")
    )
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not SAFE_TEST_DB,
        reason="需要数据库名含 test 的 COLORCHASE_TEST_DATABASE_URL",
    ),
]


def _run(coro):
    return asyncio.run(coro)


async def _setup():
    engine = create_async_engine(DB_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    User.__table__,
                    UserStorageQuota.__table__,
                    StorageQuotaReservation.__table__,
                ],
                checkfirst=True,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    marker = "quota_it_" + uuid.uuid4().hex
    async with factory() as session:
        user = User(email=f"{marker}@example.test", storage_label=marker, role="user")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return engine, factory, marker, user.id


async def _teardown(engine, factory, marker, user_id):
    async with factory() as session:
        await session.execute(
            delete(StorageQuotaReservation).where(StorageQuotaReservation.user_id == user_id)
        )
        await session.execute(delete(UserStorageQuota).where(UserStorageQuota.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
    await engine.dispose()


def test_create_all_and_model_tables_exist():
    async def run():
        engine, factory, marker, user_id = await _setup()
        try:
            async with engine.connect() as conn:
                names = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = DATABASE()"
                            )
                        )
                    ).all()
                }
            assert "user_storage_quotas" in names
            assert "storage_quota_reservations" in names
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())


def test_get_lock_release_uses_same_connection_and_leaves_no_pool_lock(monkeypatch):
    async def run():
        engine, factory, marker, user_id = await _setup()
        monkeypatch.setattr(sq, "get_engine", lambda: engine)
        key = sq._hash_lock_key(sq._quota_row_lock_key(user_id, "training"))
        try:
            first = await sq.acquire_quota_row_lock(user_id=user_id, kind="training", timeout=1)
            assert first.acquired
            second = await sq.acquire_quota_row_lock(user_id=user_id, kind="training", timeout=0)
            assert not second.acquired
            assert second._conn is None
            await first.release()
            await first.release()
            third = await sq.acquire_quota_row_lock(user_id=user_id, kind="training", timeout=1)
            assert third.acquired
            await third.release()
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT IS_USED_LOCK(:k)"), {"k": key})).scalar() is None
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())


def test_first_concurrent_usage_creates_one_quota_row(monkeypatch):
    async def run():
        engine, factory, marker, user_id = await _setup()
        monkeypatch.setattr(sq, "get_engine", lambda: engine)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
        try:
            async def record():
                async with factory() as session:
                    return await sq.record_usage(
                        session, user_id=user_id, kind="training", delta_bytes=100
                    )

            await asyncio.gather(record(), record())
            async with factory() as session:
                rows = (
                    await session.execute(
                        select(UserStorageQuota).where(
                            UserStorageQuota.user_id == user_id,
                            UserStorageQuota.kind == "training",
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].used_bytes == 200
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())


def test_same_sample_operation_lock_is_mutually_exclusive(monkeypatch):
    async def run():
        engine, factory, marker, user_id = await _setup()
        monkeypatch.setattr(sq, "get_engine", lambda: engine)
        entered = asyncio.Event()
        release = asyncio.Event()
        second_entered = False

        async def first():
            async with sq.quota_operation_lock(user_id, "training", "same"):
                entered.set()
                await release.wait()

        async def second():
            nonlocal second_entered
            await entered.wait()
            async with sq.quota_operation_lock(user_id, "training", "same", timeout=2):
                second_entered = True

        try:
            first_task = asyncio.create_task(first())
            second_task = asyncio.create_task(second())
            await entered.wait()
            await asyncio.sleep(0.2)
            assert second_entered is False
            release.set()
            await asyncio.gather(first_task, second_task)
            assert second_entered is True
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())


def test_settle_release_idempotence_and_reconcile_state(monkeypatch, tmp_path):
    async def run():
        engine, factory, marker, user_id = await _setup()
        monkeypatch.setattr(sq, "get_engine", lambda: engine)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", False)
        monkeypatch.setattr(sq, "quota_sample_root", lambda kind, label: tmp_path / kind)
        root = tmp_path / "training"
        sample = root / "sample"
        sample.mkdir(parents=True)
        (sample / "target.jpg").write_bytes(b"t")
        (sample / "result.jpg").write_bytes(b"r")
        (sample / "meta.json").write_text("{}", encoding="utf-8")
        try:
            async with factory() as session:
                row = UserStorageQuota(
                    user_id=user_id, kind="training", used_bytes=0, reserved_bytes=0,
                    reconciled_at=sq.now_utc(), reconcile_needed=False,
                )
                session.add(row)
                await session.commit()
                async with sq.quota_operation_lock(user_id, "training", "sample") as lock:
                    reserved = await sq.reserve_quota(
                        session, user_id=user_id, role="user", kind="training",
                        sample_key="sample", incoming_bytes=3, lock_context=lock,
                    )
                    await sq.mark_reconcile_needed(
                        session, user_id=user_id, kind="training", reason="post_swap_pre_settle",
                        sample_key="sample", lock_context=lock,
                    )
                    await sq.settle_quota(
                        session, reservation_id=reserved.reservation_id, delta_bytes=3,
                        user_id=user_id, kind="training", sample_key="sample", lock_context=lock,
                    )
                    await sq.settle_quota(
                        session, reservation_id=reserved.reservation_id, delta_bytes=3,
                        user_id=user_id, kind="training", sample_key="sample", lock_context=lock,
                    )
                row = (
                    await session.execute(
                        select(UserStorageQuota).where(
                            UserStorageQuota.user_id == user_id,
                            UserStorageQuota.kind == "training",
                        )
                    )
                ).scalar_one()
                assert row.used_bytes == 3
                assert row.reserved_bytes == 0
                assert row.reconcile_needed is False
                await sq.reconcile_usage(
                    session, user_id=user_id, kind="training", storage_label=marker
                )
                assert row.reconcile_needed is False
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())


def test_release_is_idempotent(monkeypatch):
    async def run():
        engine, factory, marker, user_id = await _setup()
        monkeypatch.setattr(sq, "get_engine", lambda: engine)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", False)
        try:
            async with factory() as session:
                row = UserStorageQuota(
                    user_id=user_id, kind="training", used_bytes=0, reserved_bytes=10,
                    reconciled_at=sq.now_utc(), reconcile_needed=False,
                )
                reservation = StorageQuotaReservation(
                    reservation_id=uuid.uuid4().hex, user_id=user_id, kind="training",
                    sample_key="sample", status="reserved", requested_bytes=10,
                    expires_at=sq.now_utc() + timedelta(minutes=30),
                )
                session.add_all([row, reservation])
                await session.commit()
                async with sq.quota_operation_lock(user_id, "training", "sample") as lock:
                    await sq.release_reservation(
                        session, reservation_id=reservation.reservation_id,
                        user_id=user_id, kind="training", sample_key="sample",
                        lock_context=lock, stored=False,
                    )
                    await sq.release_reservation(
                        session, reservation_id=reservation.reservation_id,
                        user_id=user_id, kind="training", sample_key="sample",
                        lock_context=lock, stored=False,
                    )
                await session.refresh(row)
                await session.refresh(reservation)
                assert row.reserved_bytes == 0
                assert reservation.status == "released"
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())


def test_touch_prevents_reclaim_then_expiry_marks_reconcile(monkeypatch):
    async def run():
        engine, factory, marker, user_id = await _setup()
        monkeypatch.setattr(sq, "get_engine", lambda: engine)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_ENABLED", True)
        monkeypatch.setattr(sq, "STORAGE_QUOTA_DRY_RUN", False)
        try:
            async with factory() as session:
                row = UserStorageQuota(
                    user_id=user_id, kind="training", used_bytes=0, reserved_bytes=10,
                    reconciled_at=sq.now_utc(), reconcile_needed=False,
                )
                reservation = StorageQuotaReservation(
                    reservation_id=uuid.uuid4().hex, user_id=user_id, kind="training",
                    sample_key="sample", status="reserved", requested_bytes=10,
                    expires_at=sq.now_utc() + timedelta(minutes=1),
                )
                session.add_all([row, reservation])
                await session.commit()
                assert await sq.touch_reservation(
                    session, reservation_id=reservation.reservation_id, extend_minutes=5
                )
                first = await sq.reclaim_expired_reservations(
                    session, now=sq.now_utc() + timedelta(minutes=2)
                )
                assert first.processed == 0
                second = await sq.reclaim_expired_reservations(
                    session, now=sq.now_utc() + timedelta(minutes=10)
                )
                assert second.processed == 1
                await session.refresh(row)
                await session.refresh(reservation)
                assert reservation.status == "expired"
                assert row.reconcile_needed is True
                assert row.reconcile_reason == "reservation_expired"
        finally:
            await _teardown(engine, factory, marker, user_id)
    _run(run())
