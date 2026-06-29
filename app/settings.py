import os
from datetime import timedelta, timezone


ENVIRONMENT = os.environ.get("COLORCHASE_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in {"prod", "production"}
USER_SPACE_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

DEFAULT_ALLOWED_ORIGINS = (
    "https://colorchase.meiyoutou.top",
    "https://ColorChase.meiyoutou.top",
)

DEFAULT_ALLOWED_HOSTS = (
    "colorchase.meiyoutou.top",
    "ColorChase.meiyoutou.top",
)


def allowed_origins():
    raw = os.environ.get("COLORCHASE_ALLOWED_ORIGINS", "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if IS_PRODUCTION:
        return list(DEFAULT_ALLOWED_ORIGINS)
    return ["*"]


def allowed_hosts():
    raw = os.environ.get("COLORCHASE_ALLOWED_HOSTS", "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if IS_PRODUCTION:
        return list(DEFAULT_ALLOWED_HOSTS)
    return ["*"]


def int_env(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, default)), 1)
    except (TypeError, ValueError):
        return default
