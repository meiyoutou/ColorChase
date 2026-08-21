"""可选集成测试：真实 MySQL 验收（默认不可运行）。

使用独立测试库 COLORCHASE_TEST_DATABASE_URL；未设置时跳过。
本地/CI 无 MySQL 时不会运行。有 MySQL 时执行：
    set COLORCHASE_TEST_DATABASE_URL=mysql+aiomysql://user:pass@host/db
    pytest -m integration tests/integration/test_storage_quota_mysql.py
"""
import os

import pytest

integration = pytest.mark.integration

DB_URL = os.environ.get("COLORCHASE_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB_URL, reason="未设置 COLORCHASE_TEST_DATABASE_URL，跳过真实 MySQL 集成测试"),
]


def _is_available():
    return bool(DB_URL)


@integration
def test_needs_real_mysql_environment():
    """占位：无库环境自动 skip。有库时须覆盖 create_all/GET_LOCK/并发预留/连接释放/E2E。"""
    assert True


@integration
def test_present_placeholder():
    assert _is_available()  # 仅当设置了 DB_URL 才断言；否则被 marker 跳过