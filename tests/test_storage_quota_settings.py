"""存储配额配置读取的单元测试。

验证 bool_env / non_negative_int_env 与统一开关命名在默认情况下全部关闭，
且 0/非法值处理正确，保证启用前对现有行为零影响。
"""
import importlib
import sys


def _reload_settings(monkeypatch, env: dict):
    import app.settings as settings

    for key in list(env):
        monkeypatch.setenv(key, env[key])
    for key in list(env):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "os", settings.os)  # no-op to keep lint happy
    return importlib.reload(settings)


def test_default_quota_switches_are_all_off(monkeypatch):
    import app.settings as settings

    monkeypatch.delenv("COLORCHASE_USER_STORAGE_QUOTA_ENABLED", raising=False)
    monkeypatch.delenv("COLORCHASE_TRAINING_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("COLORCHASE_QUOTA_DRY_RUN", raising=False)
    settings = importlib.reload(settings)

    assert settings.STORAGE_QUOTA_ENABLED is False
    assert settings.STORAGE_QUOTA_DRY_RUN is True
    assert settings.TRAINING_RETENTION_DAYS == 0


def test_bool_env_only_true_for_explicit_true_values(monkeypatch):
    from app.settings import bool_env

    for value in ("1", "true", "TRUE", "yes", "Yes", "on", " ON "):
        monkeypatch.setenv("T", value)
        assert bool_env("T") is True, value

    for value in ("0", "false", "no", "off", "", "2", "abc"):
        monkeypatch.setenv("T", value)
        assert bool_env("T") is False, value

    monkeypatch.delenv("T", raising=False)
    assert bool_env("T") is False
    assert bool_env("T", default=True) is True


def test_non_negative_int_env_allows_zero_and_rejects_negative(monkeypatch):
    from app.settings import non_negative_int_env

    monkeypatch.setenv("N", "0")
    assert non_negative_int_env("N", default=7) == 0

    monkeypatch.setenv("N", "42")
    assert non_negative_int_env("N", default=7) == 42

    monkeypatch.setenv("N", "-5")
    assert non_negative_int_env("N", default=7) == 7

    monkeypatch.setenv("N", "not-a-number")
    assert non_negative_int_env("N", default=7) == 7

    monkeypatch.delenv("N", raising=False)
    assert non_negative_int_env("N", default=7) == 7