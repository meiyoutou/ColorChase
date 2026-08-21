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


def bool_env(name: str, default: bool = False) -> bool:
    """布尔开关读取：仅 1/true/yes/on（忽略大小写）解析为真，其余为假。

    与 int_env 不同，这里不把 0 当 1 处理，用于配额等需要显式区分「关闭」的开关。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def non_negative_int_env(name: str, default: int = 0) -> int:
    """非负整数读取：允许 0，非法值回退到 default。

    用于配额大小、保留天数等取值可为 0 的配置，与 int_env（最小限 1）语义分离。
    """
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)
    return value if value >= 0 else int(default)


# ---------------------------------------------------------------
# 用户存储配额 / 过期清理 —— 统一开关命名。
# 全部默认关闭（QUOTA_ENABLED=False），启用前对现有行为零影响。
# 启用路径三档：
#   - 全关（默认）：bool_env(QUOTA_ENABLED)=False，服务层所有函数 no-op
#   - 观测模式：   QUOTA_ENABLED=True + DRY_RUN=True → 只记日志/计数，不拒绝不删除
#   - 强制模式：   QUOTA_ENABLED=True + DRY_RUN=False → 超限 413，清理真实删除
# ---------------------------------------------------------------
STORAGE_QUOTA_ENABLED = bool_env("COLORCHASE_USER_STORAGE_QUOTA_ENABLED", default=False)
STORAGE_QUOTA_DRY_RUN = bool_env("COLORCHASE_QUOTA_DRY_RUN", default=True)
TRAINING_RETENTION_DAYS = non_negative_int_env("COLORCHASE_TRAINING_RETENTION_DAYS", default=0)
TRAINING_QUOTA_MB = non_negative_int_env("COLORCHASE_TRAINING_QUOTA_MB", default=500)
DETECTION_QUOTA_MB = non_negative_int_env("COLORCHASE_DETECTION_QUOTA_MB", default=500)

# 配额种类白名单（服务层校验，防止任意字符串当 kind 写进唯一索引）
STORAGE_QUOTA_KINDS = ("training", "detection")
