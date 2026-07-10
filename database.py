"""Database configuration.

ColorChase requires MySQL in every environment. Set
COLORCHASE_DATABASE_URL explicitly; SQLite fallback is intentionally not
supported.
"""
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base


def _load_database_url() -> str:
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("必须配置 COLORCHASE_DATABASE_URL，本地和生产都必须使用 MySQL")
    if not url.startswith(("mysql+aiomysql://", "mysql+pymysql://")):
        raise RuntimeError("COLORCHASE_DATABASE_URL 必须使用 mysql+aiomysql:// 或 mysql+pymysql://")
    return url


DATABASE_URL = _load_database_url()

engine = None
_session_factory = None


def get_engine():
    global engine
    if engine is None:
        engine = create_async_engine(DATABASE_URL, echo=False)
    return engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _session_factory


class _LazyAsyncSession:
    def __call__(self, *args, **kwargs):
        return _get_session_factory()(*args, **kwargs)


async_session = _LazyAsyncSession()
Base = declarative_base()


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
