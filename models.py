from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(64), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=True)
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


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    file_name = Column(String(512))
    rating = Column(Integer, default=0)
