import asyncio
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from app.services import user_identity


@dataclass
class UserRow:
    id: int
    email: str | None = None
    phone: str | None = None
    storage_label: str | None = None


class FakeResult:
    def __init__(self, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return self._scalar


class FakeDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.storage_label = None
        self.execute_count = 0
        self.update_labels = []
        self.commit_count = 0
        self.concurrent_reads = False
        self.read_count = 0
        self.read_event = asyncio.Event()

    def next_row(self):
        if self.rows:
            row = self.rows.pop(0)
            if row.storage_label:
                self.storage_label = row.storage_label
            return row
        return UserRow(id=1, email="test@example.com", storage_label=self.storage_label)


class FakeSession:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None):
        self.db.execute_count += 1
        sql = str(getattr(statement, "text", statement))
        if sql.startswith("UPDATE users SET storage_label"):
            label = params["label"]
            self.db.update_labels.append(label)
            if self.db.storage_label is None:
                self.db.storage_label = label
            return FakeResult()

        if "users.id" in sql and "users.email" in sql:
            if self.db.concurrent_reads:
                self.db.read_count += 1
                if self.db.read_count >= 2:
                    self.db.read_event.set()
                await asyncio.wait_for(self.db.read_event.wait(), timeout=1)
            return FakeResult(row=self.db.next_row())

        if "users.storage_label" in sql:
            return FakeResult(scalar=self.db.storage_label)

        return FakeResult()

    async def commit(self):
        self.db.commit_count += 1


class FakeSessionFactory:
    def __init__(self, db):
        self.db = db
        self.created = 0

    def __call__(self):
        self.created += 1
        return FakeSession(self.db)


@pytest.fixture(autouse=True)
def clear_cache():
    user_identity.clear_user_storage_label_cache()


def patch_session(monkeypatch, db):
    factory = FakeSessionFactory(db)
    monkeypatch.setattr(user_identity, "async_session", factory)
    return factory


def test_email_generates_storage_label(monkeypatch):
    db = FakeDb([UserRow(id=1, email="test@example.com")])
    patch_session(monkeypatch, db)

    assert asyncio.run(user_identity.resolve_user_storage_label(1)) == "user_test@example.com"
    assert db.storage_label == "user_test@example.com"


def test_phone_generates_storage_label(monkeypatch):
    db = FakeDb([UserRow(id=1, phone="13800138000")])
    patch_session(monkeypatch, db)

    assert asyncio.run(user_identity.resolve_user_storage_label(1)) == "user_13800138000"


def test_email_preferred_over_phone(monkeypatch):
    db = FakeDb([UserRow(id=1, email="first@example.com", phone="13800138000")])
    patch_session(monkeypatch, db)

    assert asyncio.run(user_identity.resolve_user_storage_label(1)) == "user_first@example.com"


def test_missing_email_and_phone_raises(monkeypatch):
    db = FakeDb([UserRow(id=1)])
    patch_session(monkeypatch, db)

    with pytest.raises(HTTPException):
        asyncio.run(user_identity.resolve_user_storage_label(1))


def test_invalid_chars_are_replaced():
    assert user_identity.build_user_storage_label(" Name+中文@example.com ") == "user_name_@example.com"


def test_existing_storage_label_returns_without_write(monkeypatch):
    db = FakeDb([UserRow(id=1, email="test@example.com", storage_label="user_existing")])
    patch_session(monkeypatch, db)

    assert asyncio.run(user_identity.resolve_user_storage_label(1)) == "user_existing"
    assert db.update_labels == []


def test_cache_hit_skips_second_db_roundtrip(monkeypatch):
    db = FakeDb([UserRow(id=1, email="test@example.com")])
    factory = patch_session(monkeypatch, db)

    assert asyncio.run(user_identity.resolve_user_storage_label(1)) == "user_test@example.com"
    assert asyncio.run(user_identity.resolve_user_storage_label(1)) == "user_test@example.com"
    assert factory.created == 1


def test_concurrent_write_does_not_overwrite_existing_label(monkeypatch):
    db = FakeDb([
        UserRow(id=1, email="first@example.com"),
        UserRow(id=1, email="second@example.com"),
    ])
    db.concurrent_reads = True
    patch_session(monkeypatch, db)

    async def run_two():
        return await asyncio.gather(
            user_identity.resolve_user_storage_label(1),
            user_identity.resolve_user_storage_label(1),
        )

    result = asyncio.run(run_two())

    assert db.update_labels == ["user_first@example.com", "user_second@example.com"]
    assert db.storage_label == "user_first@example.com"
    assert result == ["user_first@example.com", "user_first@example.com"]
