"""批量配额对账脚本的默认关闭安全测试。"""
import asyncio

from scripts import reconcile_storage_quotas as script


def test_bulk_reconcile_default_off_never_opens_database(monkeypatch, capsys):
    monkeypatch.setattr(script, "STORAGE_QUOTA_ENABLED", False)

    class ForbiddenSession:
        def __call__(self):
            raise AssertionError("database must not be opened while quota is disabled")

    monkeypatch.setattr(script, "async_session", ForbiddenSession())
    asyncio.run(script.main())

    assert "disabled" in capsys.readouterr().out.lower()
