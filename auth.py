import os
from datetime import datetime, timedelta
from pathlib import Path
import bcrypt
from jose import jwt

DEFAULT_SECRET_KEY = "colorchase-local-fixed-secret-key-2026"
AUTH_COOKIE_NAME = "cc_access_token"


def _load_local_env_defaults() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    except Exception:
        return


def _read_env_value(name: str) -> str:
    raw = os.environ.get(name)
    if raw is not None and str(raw).strip():
        return str(raw).strip().strip('"').strip("'")

    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


_load_local_env_defaults()
ENVIRONMENT = _read_env_value("COLORCHASE_ENV") or "development"
ENVIRONMENT = ENVIRONMENT.strip().lower()
IS_PRODUCTION = ENVIRONMENT in {"prod", "production"}
SECRET_KEY = _read_env_value("COLORCHASE_SECRET_KEY")
if IS_PRODUCTION and not SECRET_KEY:
    raise RuntimeError("COLORCHASE_SECRET_KEY must be set in production")
if not SECRET_KEY:
    SECRET_KEY = DEFAULT_SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440


def get_password_hash(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password, hashed_password):
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
