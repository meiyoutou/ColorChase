import json
import os
import random
import smtplib
import socket
import time
import shutil
from html import escape
from pathlib import Path
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header as EmailHeader
from email.utils import formataddr
from urllib.parse import urlparse
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import (
    ALGORITHM,
    AUTH_COOKIE_NAME,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SECRET_KEY,
    create_access_token,
    get_password_hash,
    verify_password,
)
from config import (
    BASE_DIR,
    STORAGE_IMAGE_DEBUG_DIR,
    STORAGE_IMAGE_LUT_DIR,
    STORAGE_IMAGE_UPLOADS_DIR,
    STORAGE_PROJECT_ASSETS_DIR,
    STORAGE_TRAINING_CORPUS_DIR,
    STORAGE_USER_IMAGES_DIR,
    STORAGE_USER_PROFILES_DIR,
    STORAGE_USER_REFERENCES_DIR,
    STORAGE_USERS_DIR,
    STORAGE_VIDEO_FRAMES_DIR,
    STORAGE_VIDEO_UPLOADS_DIR,
    STORAGE_VIDEOS_DIR,
    get_image_debug_dir,
    get_image_lut_dir,
    get_image_upload_dir,
    get_temp_lut_dir,
    get_upload_dir,
    get_video_dir,
    get_video_frames_dir,
    get_video_result_dir,
    get_video_upload_dir,
    get_user_assets_dir,
    get_user_images_dir,
    get_user_profiles_dir,
    get_user_references_dir,
)
from database import get_db
from models import Asset, Project, User

router = APIRouter()

_verify_cache = {}
ENVIRONMENT = os.environ.get("COLORCHASE_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in {"prod", "production"}
VERIFY_PURPOSE_REGISTER = "register"
VERIFY_PURPOSE_DELETE = "delete_account"
VERIFY_PURPOSE_CHANGE_PASSWORD = "change_password"
USER_ASSET_DIR = get_user_assets_dir()
USER_LOCAL_DIR = USER_ASSET_DIR / "local_user"
USER_LOCAL_IMAGE_DIR = get_user_images_dir()
USER_LOCAL_REFERENCE_DIR = get_user_references_dir()
USER_LOCAL_PROFILE_DIR = get_user_profiles_dir()


def _auth_cookie_secure() -> bool:
    return IS_PRODUCTION


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=_auth_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        secure=_auth_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _extract_request_token(
    authorization: Optional[str],
    access_token_cookie: Optional[str],
) -> str:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="认证格式错误")
        return token
    if access_token_cookie:
        return access_token_cookie
    raise HTTPException(status_code=401, detail="缺少认证令牌")


def _runtime_image_upload_dir() -> Path:
    return get_image_upload_dir()


def _runtime_image_lut_dir() -> Path:
    return get_image_lut_dir()


def _runtime_image_debug_dir() -> Path:
    return get_image_debug_dir()


def _runtime_video_upload_dir() -> Path:
    return get_video_upload_dir()


def _runtime_video_result_dir() -> Path:
    return get_video_result_dir()


def _runtime_video_frames_dir() -> Path:
    return get_video_frames_dir()


def _runtime_temp_lut_dir() -> Path:
    return get_temp_lut_dir()


def _runtime_upload_dir() -> Path:
    return get_upload_dir()


def _runtime_video_dir() -> Path:
    return get_video_dir()


def _read_local_env(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is not None and str(raw).strip():
        return str(raw).strip().strip('"').strip("'")

    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return default

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key.strip() == name:
                cleaned = value.strip().strip('"').strip("'")
                return cleaned or default
    except Exception:
        pass

    return default


def _smtp_host_candidates(primary_host: str):
    seen = set()
    for raw_host in (primary_host, "smtp.qq.com", "mp.mail.qq.com"):
        host = str(raw_host or "").strip().strip('"').strip("'")
        if host.startswith("smtp://"):
            host = host[7:]
        elif host.startswith("smtps://"):
            host = host[8:]
        host = host.split("/", 1)[0]
        if host.count(":") == 1:
            name, port = host.rsplit(":", 1)
            if port.isdigit():
                host = name
        if host and host not in seen:
            seen.add(host)
            yield host


def _cleanup_roots():
    roots = []
    for item in (
        BASE_DIR,
        USER_ASSET_DIR,
        STORAGE_USERS_DIR,
        STORAGE_IMAGE_UPLOADS_DIR,
        STORAGE_IMAGE_LUT_DIR,
        STORAGE_IMAGE_DEBUG_DIR,
        STORAGE_VIDEO_UPLOADS_DIR,
        STORAGE_VIDEOS_DIR,
        STORAGE_VIDEO_FRAMES_DIR,
        STORAGE_PROJECT_ASSETS_DIR,
        STORAGE_TRAINING_CORPUS_DIR,
        _runtime_image_upload_dir(),
        _runtime_image_lut_dir(),
        _runtime_image_debug_dir(),
        _runtime_video_upload_dir(),
        _runtime_video_result_dir(),
        _runtime_video_frames_dir(),
        _runtime_temp_lut_dir(),
    ):
        try:
            roots.append(Path(item).resolve())
        except Exception:
            continue
    return roots


def _clean_expired():
    now = time.time()
    for key in list(_verify_cache.keys()):
        if _verify_cache[key]["expire"] < now:
            del _verify_cache[key]


def _verify_cache_key(purpose: str, identifier: str) -> str:
    return f"{purpose}:{str(identifier or '').strip().lower()}"


def _build_verify_email(code: str, purpose: str = VERIFY_PURPOSE_REGISTER):
    safe_code = escape(str(code or ""))
    if purpose == VERIFY_PURPOSE_DELETE:
        subject = "[ColorChase] 注销账户验证码"
        badge = "ColorChase 账户安全确认"
        headline = "请确认你的账户注销操作"
        desc = "你正在申请注销 ColorChase 账号。请输入下方 6 位数字验证码，以完成本次账户安全确认。"
        finish_text = "请在 5 分钟内完成账户注销确认"
        use_text = "仅用于本次账户注销"
        input_tip = "请将以下 6 位数字验证码输入到注销确认窗口"
        plain_body = (
            f"{subject}\n\n"
            f"你正在进行 ColorChase 账户注销操作。\n"
            f"本次验证码为：{code}\n"
            f"验证码 5 分钟内有效，且仅可用于本次账户注销确认。\n\n"
            f"安全提醒：\n"
            f"1. 请勿将验证码泄露给任何人。\n"
            f"2. ColorChase 工作人员不会以任何理由向你索取验证码。\n"
            f"3. 如果这不是你的操作，请立即修改密码并忽略本邮件。"
        )
    elif purpose == VERIFY_PURPOSE_CHANGE_PASSWORD:
        subject = "[ColorChase] 修改密码验证码"
        badge = "ColorChase 密码修改确认"
        headline = "请确认你的密码修改操作"
        desc = "你正在修改 ColorChase 账号密码。请输入下方 6 位数字验证码，以完成本次安全确认。"
        finish_text = "请在 5 分钟内完成密码修改确认"
        use_text = "仅用于本次密码修改"
        input_tip = "请将以下 6 位数字验证码输入到修改密码窗口"
        plain_body = (
            f"{subject}\n\n"
            f"你正在进行 ColorChase 密码修改操作。\n"
            f"本次验证码为：{code}\n"
            f"验证码 5 分钟内有效，且仅可用于本次密码修改确认。\n\n"
            f"安全提醒：\n"
            f"1. 请勿将验证码泄露给任何人。\n"
            f"2. ColorChase 工作人员不会以任何理由向你索取验证码。\n"
            f"3. 如果这不是你的操作，请立即检查账户安全并忽略本邮件。"
        )
    else:
        subject = "[ColorChase] 注册验证码"
        badge = "ColorChase 注册验证"
        headline = "请完成你的账号验证"
        desc = "你正在注册 ColorChase 账号。请在有效时间内输入下方 6 位数字验证码，完成本次注册验证。"
        finish_text = "请在 5 分钟内完成注册验证"
        use_text = "仅用于本次注册验证"
        input_tip = "请将以下 6 位数字验证码输入到注册页面"
        plain_body = (
            f"{subject}\n\n"
            f"你正在进行 ColorChase 账号注册。\n"
            f"本次验证码为：{code}\n"
            f"验证码 5 分钟内有效，且仅可用于本次注册验证。\n\n"
            f"安全提醒：\n"
            f"1. 请勿将验证码泄露给任何人。\n"
            f"2. ColorChase 工作人员不会以任何理由向你索取验证码。\n"
            f"3. 如果这不是你的操作，请直接忽略本邮件。"
        )

    code_cells = "".join(
        f'<td align="center" valign="middle" width="38" height="50" '
        f'style="width:38px;height:50px;border-radius:13px;background:#232342;'
        f'border:1px solid #353562;box-shadow:inset 0 1px 0 rgba(255,255,255,0.04);'
        f'font-size:24px;line-height:1;font-weight:800;color:#f3f4ff;">{escape(ch)}</td>'
        for ch in safe_code
    )

    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#0d0d18;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#e8e8f0;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#0d0d18;">
    <tr>
      <td align="center" style="padding:20px 12px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:560px;background:#17172c;border:1px solid #2d2d4e;border-radius:28px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,0.26);">
          <tr>
            <td style="padding:28px 24px 20px;background:linear-gradient(160deg,#16162a 0%,#1f1f39 55%,#252540 100%);">
              <div style="display:inline-block;padding:8px 14px;border-radius:999px;background:rgba(233,69,96,0.14);border:1px solid rgba(233,69,96,0.26);font-size:12px;font-weight:700;color:#ffd9e1;">{badge}</div>
              <div style="margin-top:18px;font-size:34px;line-height:1.18;font-weight:800;color:#ffffff;">{headline}</div>
              <div style="margin-top:12px;font-size:15px;line-height:1.8;color:rgba(232,232,240,0.82);">{desc}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#20203a;border:1px solid #32325a;border-radius:22px;">
                <tr>
                  <td style="padding:22px 20px;">
                    <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#9da3c4;">Verification Code</div>
                    <div style="margin-top:10px;font-size:27px;line-height:1.35;font-weight:800;color:#ffffff;">{finish_text}</div>
                    <div style="margin-top:14px;display:inline-block;padding:9px 13px;border-radius:999px;background:rgba(233,69,96,0.12);border:1px solid rgba(233,69,96,0.22);font-size:12px;font-weight:700;color:#ffbfd0;">{use_text}</div>
                    <div style="margin-top:20px;font-size:13px;line-height:1.8;color:#a6abc9;text-align:center;">{input_tip}</div>
                    <table role="presentation" align="center" cellspacing="4" cellpadding="0" border="0" style="margin:10px auto 0;border-collapse:separate;table-layout:fixed;white-space:nowrap;">
                      <tr>{code_cells}</tr>
                    </table>
                  </td>
                </tr>
              </table>

              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:16px;background:#20203a;border:1px solid #32325a;border-radius:18px;">
                <tr>
                  <td style="padding:16px 18px;">
                    <div style="font-size:12px;color:#9da3c4;">有效时长</div>
                    <div style="margin-top:8px;font-size:22px;font-weight:800;color:#ffffff;">5 分钟</div>
                  </td>
                </tr>
              </table>


              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:16px;background:rgba(233,69,96,0.10);border:1px solid rgba(233,69,96,0.18);border-radius:18px;">
                <tr>
                  <td style="padding:16px 18px;font-size:13px;line-height:1.85;color:#d5d8e8;">
                    安全提醒：请勿将验证码泄露给任何人，ColorChase 工作人员不会以任何理由向你索取验证码。
                  </td>
                </tr>
              </table>

              <div style="margin-top:18px;padding-top:18px;border-top:1px solid #2f2f52;font-size:13px;line-height:1.9;color:#9da3c4;">
                如果这不是你的操作，请直接忽略本邮件。<br>
                本邮件由系统自动发送，请勿直接回复。
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return subject, plain_body, html_body

def _send_email(to_email: str, code: str, purpose: str = VERIFY_PURPOSE_REGISTER):
    smtp_host = _read_local_env("CC_SMTP_HOST", "smtp.qq.com")
    smtp_user = _read_local_env("CC_SMTP_USER", "")
    smtp_pass = _read_local_env("CC_SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        print(f"[验证码] {to_email} -> {code}")
        return

    subject, plain_body, html_body = _build_verify_email(code, purpose=purpose)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((str(EmailHeader("没有头", "utf-8")), smtp_user))
    msg["To"] = to_email
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    last_err = None
    candidate_hosts = list(_smtp_host_candidates(smtp_host))
    for host in candidate_hosts:
        for port, use_ssl in [(465, True), (587, False)]:
            try:
                if use_ssl:
                    smtp = smtplib.SMTP_SSL(host, port, timeout=10)
                else:
                    smtp = smtplib.SMTP(host, port, timeout=10)
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(smtp_user, smtp_pass)
                smtp.sendmail(smtp_user, [to_email], msg.as_string())
                smtp.quit()
                return
            except Exception as exc:
                last_err = exc
                continue

    if isinstance(last_err, socket.gaierror):
        tried_hosts = ", ".join(candidate_hosts) or smtp_host
        raise Exception(f"SMTP 主机解析失败，请检查网络或主机配置：{tried_hosts}")
    raise last_err or Exception("SMTP 连接失败")


class SendCodeRequest(BaseModel):
    email: str


@router.post("/send_code")
async def send_verify_code(req: SendCodeRequest):
    email = req.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请提供有效的邮箱地址")

    _clean_expired()
    cache_key = _verify_cache_key(VERIFY_PURPOSE_REGISTER, email)
    if cache_key in _verify_cache:
        remaining = int(_verify_cache[cache_key]["expire"] - time.time())
        if remaining > 240:
            raise HTTPException(status_code=429, detail=f"请在 {remaining - 240} 秒后再发送")

    code = str(random.randint(100000, 999999))
    _verify_cache[cache_key] = {"code": code, "expire": time.time() + 300}

    smtp_configured = bool(_read_local_env("CC_SMTP_USER"))
    if smtp_configured:
        try:
            _send_email(email, code, purpose=VERIFY_PURPOSE_REGISTER)
        except Exception as exc:
            del _verify_cache[cache_key]
            raise HTTPException(status_code=500, detail=f"邮件发送失败: {str(exc)}")
        return {"message": "验证码已发送，5 分钟内有效"}

    if IS_PRODUCTION:
        del _verify_cache[cache_key]
        raise HTTPException(status_code=500, detail="Email verification is not configured")

    print(f"[验证码] {email} -> {code}")
    return {"message": "验证码已发送", "code": code}


class RegisterRequest(BaseModel):
    email: Optional[str] = None
    password: str
    code: str = ""


class LoginRequest(BaseModel):
    account: str
    password: str


class SendDeleteCodeRequest(BaseModel):
    password: str


class DeleteAccountRequest(BaseModel):
    password: str
    confirm_text: str
    code: str


class SendChangePasswordCodeRequest(BaseModel):
    old_password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    code: str
    new_password: str
    confirm_password: str


@router.post("/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if not req.email:
        raise HTTPException(status_code=400, detail="请提供邮箱地址")
    if not req.code:
        raise HTTPException(status_code=400, detail="请输入验证码")

    email = req.email.strip().lower()
    _clean_expired()
    cached = _verify_cache.get(_verify_cache_key(VERIFY_PURPOSE_REGISTER, email))
    if not cached or cached["code"] != req.code:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    del _verify_cache[_verify_cache_key(VERIFY_PURPOSE_REGISTER, email)]

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    hashed = get_password_hash(req.password)
    user = User(email=email, hashed_password=hashed, role="user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {"message": "注册成功", "user_id": user.id}


@router.post("/login")
async def login(
    req: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where((User.phone == req.account) | (User.email == req.account))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="账号不存在")

    if not user.hashed_password:
        raise HTTPException(status_code=401, detail="该账号未设置密码，请使用第三方登录")

    if not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="密码错误")

    token = create_access_token({"sub": str(user.id), "role": user.role})
    _set_auth_cookie(response, token)
    return {
        "access_token": token,
        "token_type": "bearer",
        "id": user.id,
        "phone": user.phone,
        "email": user.email,
        "role": user.role,
    }


def _remove_if_exists(path: Path):
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _resolve_within_cleanup_roots(value):
    try:
        resolved = Path(value).resolve()
    except Exception:
        return None
    for root in _cleanup_roots():
        if resolved == root or root in resolved.parents:
            return resolved
    return None


def _path_within_base(value):
    if not isinstance(value, (str, Path)):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    candidate = Path(raw)
    return _resolve_within_cleanup_roots(candidate) if candidate.is_absolute() else None


def _resolve_virtual_asset_path(value):
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    route_path = (parsed.path or raw).split("?", 1)[0].replace("\\", "/")
    if route_path.startswith("/api/user_assets/"):
        parts = route_path[len("/api/user_assets/"):].split("/", 1)
        user_roots = {
            "images": USER_LOCAL_IMAGE_DIR,
            "references": USER_LOCAL_REFERENCE_DIR,
            "profiles": USER_LOCAL_PROFILE_DIR,
        }
        if len(parts) == 2 and parts[0] in user_roots:
            return _resolve_within_cleanup_roots(user_roots[parts[0]] / Path(parts[1]))
        return None
    route_roots = {
        "/assets/": USER_ASSET_DIR,
        "/videos/": _runtime_video_result_dir(),
        "/styles/": BASE_DIR / "styles",
    }
    for prefix, root in route_roots.items():
        if not route_path.startswith(prefix):
            continue
        relative = route_path[len(prefix):].strip("/")
        if not relative:
            return None
        candidate = root / Path(relative)
        return _resolve_within_cleanup_roots(candidate)
    return None


def _add_named_cleanup_candidates(value, bucket):
    if not isinstance(value, str):
        return
    raw = value.strip()
    if not raw:
        return
    parsed = urlparse(raw)
    name = Path((parsed.path or raw).split("?", 1)[0]).name
    if not name:
        return
    for root in (
        _runtime_image_upload_dir(),
        _runtime_video_upload_dir(),
        _runtime_video_result_dir(),
        _runtime_image_lut_dir(),
        USER_LOCAL_IMAGE_DIR,
        USER_LOCAL_REFERENCE_DIR,
        USER_LOCAL_PROFILE_DIR,
    ):
        candidate = _resolve_within_cleanup_roots(Path(root) / name)
        if candidate is not None:
            bucket.add(candidate)


def _add_session_cleanup(session_id, bucket):
    sid = str(session_id or "").strip()
    if not sid:
        return
    for candidate in (
        _runtime_temp_lut_dir() / sid,
        _runtime_temp_lut_dir() / f"{sid}.npy",
        _runtime_temp_lut_dir() / f"{sid}_result_preview.jpg",
        _runtime_temp_lut_dir() / f"{sid}_orig_preview.jpg",
    ):
        resolved = _resolve_within_cleanup_roots(candidate)
        if resolved is not None:
            bucket.add(resolved)


def _add_cleanup_value(value, bucket):
    resolved = _path_within_base(value)
    if resolved is not None:
        bucket.add(resolved)
        return
    resolved = _resolve_virtual_asset_path(value)
    if resolved is not None:
        bucket.add(resolved)
        return
    _add_named_cleanup_candidates(value, bucket)


def _collect_snapshot_paths(value, bucket):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"sessionId", "mergedSessionId", "profileSessionId", "ai_session_id", "profile_session_id"}:
                _add_session_cleanup(item, bucket)
            else:
                _collect_snapshot_paths(item, bucket)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_snapshot_paths(item, bucket)
        return
    _add_cleanup_value(value, bucket)


async def get_current_user(
    authorization: Optional[str] = Header(None),
    access_token_cookie: Optional[str] = Cookie(None, alias=AUTH_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
):
    token = _extract_request_token(authorization, access_token_cookie)

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="令牌无效或已过期")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")

    now = datetime.utcnow()
    if user.last_active_at is None or now - user.last_active_at >= timedelta(minutes=30):
        user.last_active_at = now
        await db.commit()

    return user


@router.post("/logout")
async def logout(response: Response):
    _clear_auth_cookie(response)
    return {"message": "已退出登录"}


async def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.post("/send_delete_code")
async def send_delete_code(
    req: SendDeleteCodeRequest,
    user: User = Depends(get_current_user),
):
    if user.role == "admin":
        raise HTTPException(status_code=403, detail="管理员账号禁止注销")
    if not user.email:
        raise HTTPException(status_code=400, detail="当前账户未绑定邮箱，暂不支持邮箱二次验证注销")
    if not user.hashed_password:
        raise HTTPException(status_code=400, detail="当前账户未设置密码，暂不支持直接注销")

    password = (req.password or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="请输入当前账户密码")
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="账户密码错误")

    email = str(user.email or "").strip().lower()
    _clean_expired()
    cache_key = _verify_cache_key(VERIFY_PURPOSE_DELETE, email)
    if cache_key in _verify_cache:
        remaining = int(_verify_cache[cache_key]["expire"] - time.time())
        if remaining > 240:
            raise HTTPException(status_code=429, detail=f"请在 {remaining - 240} 秒后再发送")

    code = str(random.randint(100000, 999999))
    _verify_cache[cache_key] = {"code": code, "expire": time.time() + 300}

    smtp_configured = bool(_read_local_env("CC_SMTP_USER"))
    if smtp_configured:
        try:
            _send_email(email, code, purpose=VERIFY_PURPOSE_DELETE)
        except Exception as exc:
            del _verify_cache[cache_key]
            raise HTTPException(status_code=500, detail=f"邮件发送失败: {str(exc)}")
        return {"message": "注销验证码已发送，5 分钟内有效"}

    if IS_PRODUCTION:
        del _verify_cache[cache_key]
        raise HTTPException(status_code=500, detail="Email verification is not configured")

    print(f"[注销验证码] {email} -> {code}")
    return {"message": "注销验证码已发送", "code": code}


@router.post("/send_change_password_code")
async def send_change_password_code(
    req: SendChangePasswordCodeRequest,
    user: User = Depends(get_current_user),
):
    if not user.email:
        raise HTTPException(status_code=400, detail="当前账户未绑定邮箱，暂不支持邮箱验证码改密")
    if not user.hashed_password:
        raise HTTPException(status_code=400, detail="当前账户未设置密码，暂不支持直接修改密码")

    old_password = (req.old_password or "").strip()
    if not old_password:
        raise HTTPException(status_code=400, detail="请输入当前密码")
    if not verify_password(old_password, user.hashed_password):
        raise HTTPException(status_code=401, detail="当前密码错误")

    email = str(user.email or "").strip().lower()
    _clean_expired()
    cache_key = _verify_cache_key(VERIFY_PURPOSE_CHANGE_PASSWORD, email)
    if cache_key in _verify_cache:
        remaining = int(_verify_cache[cache_key]["expire"] - time.time())
        if remaining > 240:
            raise HTTPException(status_code=429, detail=f"请在 {remaining - 240} 秒后再发送")

    code = str(random.randint(100000, 999999))
    _verify_cache[cache_key] = {"code": code, "expire": time.time() + 300}

    smtp_configured = bool(_read_local_env("CC_SMTP_USER"))
    if smtp_configured:
        try:
            _send_email(email, code, purpose=VERIFY_PURPOSE_CHANGE_PASSWORD)
        except Exception as exc:
            del _verify_cache[cache_key]
            raise HTTPException(status_code=500, detail=f"邮件发送失败: {str(exc)}")
        return {"message": "修改密码验证码已发送，5 分钟内有效"}

    if IS_PRODUCTION:
        del _verify_cache[cache_key]
        raise HTTPException(status_code=500, detail="Email verification is not configured")

    print(f"[改密验证码] {email} -> {code}")
    return {"message": "修改密码验证码已发送", "code": code}


@router.post("/change_password")
async def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.email:
        raise HTTPException(status_code=400, detail="当前账户未绑定邮箱，暂不支持邮箱验证码改密")
    if not user.hashed_password:
        raise HTTPException(status_code=400, detail="当前账户未设置密码，暂不支持直接修改密码")

    old_password = (req.old_password or "").strip()
    code = (req.code or "").strip()
    new_password = req.new_password or ""
    confirm_password = req.confirm_password or ""

    if not old_password:
        raise HTTPException(status_code=400, detail="请输入当前密码")
    if not verify_password(old_password, user.hashed_password):
        raise HTTPException(status_code=401, detail="当前密码错误")
    if not code:
        raise HTTPException(status_code=400, detail="请输入邮箱验证码")
    if not new_password:
        raise HTTPException(status_code=400, detail="请输入新密码")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    if verify_password(new_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    email = str(user.email or "").strip().lower()
    cache_key = _verify_cache_key(VERIFY_PURPOSE_CHANGE_PASSWORD, email)
    _clean_expired()
    cached = _verify_cache.get(cache_key)
    if not cached or cached.get("code") != code:
        raise HTTPException(status_code=400, detail="邮箱验证码错误或已过期")
    del _verify_cache[cache_key]

    user.hashed_password = get_password_hash(new_password)
    await db.commit()

    return {"message": "密码已修改"}


@router.delete("/delete_account")
async def delete_account(
    req: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role == "admin":
        raise HTTPException(status_code=403, detail="管理员账号禁止注销")
    if not user.email:
        raise HTTPException(status_code=400, detail="当前账户未绑定邮箱，暂不支持邮箱二次验证注销")
    if not user.hashed_password:
        raise HTTPException(status_code=400, detail="当前账户未设置密码，暂不支持直接注销")

    password = (req.password or "").strip()
    confirm_text = (req.confirm_text or "").strip()
    code = (req.code or "").strip()

    if confirm_text != "注销账户":
        raise HTTPException(status_code=400, detail="请输入“注销账户”以确认此操作")
    if not password:
        raise HTTPException(status_code=400, detail="请输入当前账户密码")
    if not code:
        raise HTTPException(status_code=400, detail="请输入邮箱验证码")
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="账户密码错误")

    email = str(user.email or "").strip().lower()
    cache_key = _verify_cache_key(VERIFY_PURPOSE_DELETE, email)
    _clean_expired()
    cached = _verify_cache.get(cache_key)
    if not cached or cached.get("code") != code:
        raise HTTPException(status_code=400, detail="邮箱验证码错误或已过期")
    del _verify_cache[cache_key]

    result = await db.execute(select(Project).where(Project.owner_id == user.id))
    projects = result.scalars().all()
    project_ids = [item.id for item in projects]
    cleanup_paths = set()

    for project in projects:
        cleanup_paths.add(STORAGE_PROJECT_ASSETS_DIR / str(project.id))
        cleanup_paths.add(BASE_DIR / "user_assets" / "projects" / str(project.id))
        cleanup_paths.add(BASE_DIR / "uploaded" / "projects" / str(project.id))
        snapshot = project.workspace_snapshot or ""
        if snapshot:
            try:
                parsed = json.loads(snapshot)
                _collect_snapshot_paths(parsed, cleanup_paths)
            except Exception:
                pass

    if project_ids:
        asset_result = await db.execute(select(Asset).where(Asset.project_id.in_(project_ids)))
        for asset in asset_result.scalars().all():
            _add_cleanup_value(asset.file_name, cleanup_paths)
        await db.execute(delete(Asset).where(Asset.project_id.in_(project_ids)))
        await db.execute(delete(Project).where(Project.id.in_(project_ids)))

    account_tokens = [
        _verify_cache_key(VERIFY_PURPOSE_REGISTER, str(user.email or "").strip().lower()),
        _verify_cache_key(VERIFY_PURPOSE_DELETE, str(user.email or "").strip().lower()),
        _verify_cache_key(VERIFY_PURPOSE_CHANGE_PASSWORD, str(user.email or "").strip().lower()),
        _verify_cache_key(VERIFY_PURPOSE_REGISTER, str(user.phone or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_DELETE, str(user.phone or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_CHANGE_PASSWORD, str(user.phone or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_REGISTER, str(user.qq_id or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_DELETE, str(user.qq_id or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_CHANGE_PASSWORD, str(user.qq_id or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_REGISTER, str(user.wechat_id or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_DELETE, str(user.wechat_id or "").strip()),
        _verify_cache_key(VERIFY_PURPOSE_CHANGE_PASSWORD, str(user.wechat_id or "").strip()),
    ]
    for token in account_tokens:
        if token in _verify_cache:
            del _verify_cache[token]

    cleanup_paths.add(_runtime_upload_dir() / f"user_{user.id}")
    cleanup_paths.add(_runtime_video_dir() / f"user_{user.id}")
    cleanup_paths.add(STORAGE_USER_IMAGES_DIR)
    cleanup_paths.add(STORAGE_USER_REFERENCES_DIR)
    cleanup_paths.add(STORAGE_USER_PROFILES_DIR)
    cleanup_paths.add(USER_LOCAL_IMAGE_DIR)
    cleanup_paths.add(USER_LOCAL_REFERENCE_DIR)
    cleanup_paths.add(USER_LOCAL_PROFILE_DIR)
    cleanup_paths.add(_runtime_image_debug_dir())
    cleanup_paths.add(_runtime_video_frames_dir())

    for path in sorted(cleanup_paths, key=lambda p: len(str(p)), reverse=True):
        _remove_if_exists(path)

    await db.delete(user)
    await db.commit()

    return {"message": "账户已注销"}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "phone": user.phone,
        "email": user.email,
        "role": user.role,
    }
