from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func, Text
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String, unique=True, nullable=True)
    email = Column(String, unique=True, nullable=True)
    qq_id = Column(String, unique=True, nullable=True)
    wechat_id = Column(String, unique=True, nullable=True)
    hashed_password = Column(String, nullable=True)
    role = Column(String, default="user")
    created_at = Column(DateTime, server_default=func.now())
    last_active_at = Column(DateTime, nullable=True)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, default="未命名项目")
    type = Column(String)
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True, default=None)
    workspace_snapshot = Column(Text, nullable=True)


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    file_name = Column(String)
    rating = Column(Integer, default=0)
