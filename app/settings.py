import os
from datetime import timedelta, timezone
from urllib.parse import urlsplit


ENVIRONMENT = os.environ.get("COLORCHASE_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in {"prod", "production"}
USER_SPACE_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

DEFAULT_ALLOWED_ORIGINS = (
    "https://colorchase.meiyoutou.top",
    "https://meiyoutou.github.io",
)

DEFAULT_ALLOWED_HOSTS = (
    "colorchase.meiyoutou.top",
    "ColorChase.meiyoutou.top",
)


def _normalize_origin(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw or raw == "*":
        return ""

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _split_origin_list(raw: str):
    for item in raw.split(","):
        origin = _normalize_origin(item)
        if origin:
            yield origin


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def allowed_origins():
    extra_origins = _split_origin_list(os.environ.get("COLORCHASE_ALLOWED_ORIGINS", ""))
    return _dedupe([*_split_origin_list(",".join(DEFAULT_ALLOWED_ORIGINS)), *extra_origins])


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
