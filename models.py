from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.mysql import LONGTEXT
from database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ux_users_storage_label", "storage_label", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(64), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=True)
    storage_label = Column(String(128), nullable=True)
    qq_id = Column(String(128), unique=True, nullable=True)
    wechat_id = Column(String(128), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=True)
    role = Column(String(32), default="user")
    created_at = Column(DateTime, server_default=func.now())
    last_active_at = Column(DateTime, nullable=True)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), default="未命名项目")
    type = Column(String(64))
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True, default=None)
    reference_path = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    workspace_snapshot = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    local_path = Column(String(512), nullable=True)


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    file_name = Column(String(512))
    rating = Column(Integer, default=0)


class UserStorageQuota(Base):
    """按用户 / 按库种类的配额与用量核算。

    行不存在 = 使用环境变量默认配额。本行由 init_db/create_all 幂等创建，
    无论开关是否启用，建表本身无害。
    """

    __tablename__ = "user_storage_quotas"
    __table_args__ = (
        Index("ux_quota_user_kind", "user_id", "kind", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    kind = Column(String(32), nullable=False)  # "training" | "detection"
    quota_mb = Column(Integer, nullable=True)  # NULL=用环境变量默认；管理员可覆盖
    used_bytes = Column(BigInteger, nullable=False, default=0)
    reserved_bytes = Column(BigInteger, nullable=False, default=0)
    reconciled_at = Column(DateTime, nullable=True)
    reconcile_needed = Column(Boolean, nullable=False, default=False)
    reconcile_reason = Column(String(128), nullable=True)
    reconcile_sample_key = Column(String(256), nullable=True)
    reconcile_marked_at = Column(DateTime, nullable=True)
    would_deny_count = Column(Integer, nullable=False, default=0)
    denied_count = Column(Integer, nullable=False, default=0)
    last_would_deny_at = Column(DateTime, nullable=True)
    last_denied_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StorageQuotaReservation(Base):
    """配额预留明细，reservation_id 幂等 settle/release，用于并发与结算对账。"""

    __tablename__ = "storage_quota_reservations"
    __table_args__ = (
        Index("ix_reservation_user_kind_sample", "user_id", "kind", "sample_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    reservation_id = Column(String(64), unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    kind = Column(String(32), nullable=False)
    sample_key = Column(String(256), nullable=False)
    status = Column(String(16), nullable=False, default="reserved")  # reserved|settled|released
    requested_bytes = Column(BigInteger, nullable=False, default=0)
    occupied_bytes = Column(BigInteger, nullable=False, default=0)
    settled_bytes = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now())
    settled_at = Column(DateTime, nullable=True)
    released_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
