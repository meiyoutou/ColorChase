# -*- coding: utf-8 -*-
# ============================================================
#  ColorChase Local Agent  -  本地文件代理服务
#  Copyright (c) 2026 ColorChase. All Rights Reserved.
#  未经授权禁止复制、传播、逆向工程或二次分发。
#  本程序仅在用户本机运行，用于代理浏览器与本地磁盘之间的文件读写，
#  并在必要时将样本数据回传至远端服务用于计算与训练语料收集。
# ============================================================
#
#  运行方式：直接双击 ColorChaseAgent.exe，或 python colorchase_agent.py
#  监听地址：127.0.0.1:9123（仅本机访问，不对局域网开放）
#  依赖：Python 3.8+ 标准库 + requests
#

import base64
import json
import os
import re
import shutil
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests


# ============================================================
#  字符串混淆相关
#  关键字符串（远端地址、接口路径、鉴权头）都不以明文出现在源码里，
#  运行时用 XOR + base64 解出来用。这样逆向 exe 的时候不能一眼看到用途。
# ============================================================

_K = 0x37  # XOR 单字节密钥


def _d(s):
    """把 base64 字符串解出来：先 base64 解码，再逐字节 XOR 还原。"""
    raw = base64.b64decode(s)
    return bytes(b ^ _K for b in raw).decode("utf-8")


# 下面这些都是混淆后的常量，运行时才解密成真正的字符串
# 远端服务地址
_C0 = "X0NDR0QNGBhUWFtYRVRfVkRSGVpSXk5YQkNYQhlDWEc="
# 检测库上传接口
_C1 = "GFZHXhhTUkNSVENeWFkYQkdbWFZT"
# 训练库上传接口
_C2 = "GFZHXhhDRVZeWV5ZUBhCR1tYVlM="
# 鉴权头前缀
_C3 = "dVJWRVJFFw=="
# 鉴权头字段名
_C4 = "dkJDX1hFXk1WQ15YWQ=="


def _i0x1(file_path, file_uuid, token):
    """读本地原图，POST 到远端检测库接口。
    file_path 是本地完整路径，file_uuid 是文件唯一标识，token 是用户登录态。"""
    url = _d(_C0) + _d(_C1)
    headers = {_d(_C4): _d(_C3) + token}
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f)}
        data = {"file_uuid": file_uuid}
        r = requests.post(url, files=files, data=data, headers=headers, timeout=120)
    return r.status_code, r.text


def _i0x2(target_path, reference_path, result_path, meta, sample_uuid, is_video, token):
    """读本地 target/reference/result，连同 meta 一起 POST 到远端训练库接口。
    reference_path 可为空（None 表示没参考图）。"""
    url = _d(_C0) + _d(_C2)
    headers = {_d(_C4): _d(_C3) + token}
    opened = []  # 记录打开的文件句柄，最后统一关掉
    try:
        files = {}
        ft = open(target_path, "rb")
        opened.append(ft)
        files["target"] = (os.path.basename(target_path), ft)
        if reference_path:
            fr = open(reference_path, "rb")
            opened.append(fr)
            files["reference"] = (os.path.basename(reference_path), fr)
        fres = open(result_path, "rb")
        opened.append(fres)
        files["result"] = (os.path.basename(result_path), fres)
        data = {
            "sample_uuid": sample_uuid,
            "is_video": "true" if is_video else "false",
            "meta": json.dumps(meta, ensure_ascii=False),
        }
        r = requests.post(url, files=files, data=data, headers=headers, timeout=180)
        return r.status_code, r.text
    finally:
        for fobj in opened:
            try:
                fobj.close()
            except Exception:
                pass


# ============================================================
#  本地配置读写
#  配置文件放在 ~/.colorchase_agent/config.json，主要存用户设的项目地址
# ============================================================

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".colorchase_agent")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
# 简单的读写锁，避免多线程并发写坏配置文件
_cfg_lock = threading.Lock()

# 浏览器跨源访问白名单。需要本地开发时，可用逗号或空白分隔追加：
# COLORCHASE_AGENT_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
_DEFAULT_ALLOWED_ORIGINS = {
    "https://colorchase.meiyoutou.top",
    "https://meiyoutou.github.io",
}
_ALLOWED_ORIGINS_ENV = "COLORCHASE_AGENT_ALLOWED_ORIGINS"


def _load_config():
    """读配置，文件不存在或坏了就返回空字典。"""
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config(cfg):
    """写配置。"""
    with _cfg_lock:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def _log(msg):
    """简单记个日志到 ~/.colorchase_agent/agent.log，--noconsole 下也能看。"""
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        line = "[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + str(msg)
        with open(os.path.join(_CONFIG_DIR, "agent.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _normalize_origin(origin):
    """把 Origin 规整成 scheme://host[:port]，非法或 file:// 的 null 返回空。"""
    raw = str(origin or "").strip()
    if not raw or raw == "null":
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname.lower()
    netloc = host if port is None else "{}:{}".format(host, port)
    return "{}://{}".format(parsed.scheme.lower(), netloc)


def _allowed_origins():
    origins = set(_DEFAULT_ALLOWED_ORIGINS)
    extra = os.environ.get(_ALLOWED_ORIGINS_ENV, "")
    for item in re.split(r"[\s,]+", extra):
        normalized = _normalize_origin(item)
        if normalized:
            origins.add(normalized)
    return origins


def _is_allowed_origin(origin):
    # 没有 Origin 的请求通常来自本机原生命令行/桌面客户端，不走浏览器 CORS。
    raw = str(origin or "").strip()
    if not raw:
        return True
    normalized = _normalize_origin(raw)
    return bool(normalized and normalized in _allowed_origins())


# ============================================================
#  路径解析与安全校验
#  浏览器传过来的 path 是相对路径，拼到 <项目地址>/<项目名_ID>/ 下面。
#  必须防止 ../../ 之类的路径穿越，不能让网页读到项目目录以外的东西。
# ============================================================


def _sanitize_name(name):
    """清洗项目名里文件系统不允许的字符，避免建目录失败或路径混乱。"""
    # Windows 不允许 \ / : * ? " < > |，Linux/macOS 也顺手替换掉省心
    safe = re.sub(r'[\\/:*?"<>|]', '_', str(name)).strip()
    # 去掉首尾的点和空格（Windows 下目录名不能以点结尾）
    safe = safe.strip('. ')
    return safe or "unnamed"


def _resolve_path(project_id, rel_path, project_name=""):
    """把相对路径拼成绝对路径，并校验没越界。
    子目录用 <项目名_ID>，项目名为空时退回 <project_id>（兼容旧版前端）。
    返回归一化后的绝对路径；越界就抛异常。"""
    cfg = _load_config()
    base = cfg.get("project_path", "")
    if not base:
        raise ValueError("还没设置项目地址，先调 /config 设一下")
    base_norm = os.path.normpath(base)
    # 子目录名：有项目名就用 项目名_ID，没有就只 ID
    if project_name:
        sub_dir = "{}_{}".format(_sanitize_name(project_name), project_id)
    else:
        sub_dir = str(project_id)
    full = os.path.normpath(os.path.join(base_norm, sub_dir, rel_path))
    # 校验：解析后的路径必须在项目地址里面，防止穿越
    if full != base_norm and not full.startswith(base_norm + os.sep):
        raise ValueError("路径越界，不让访问项目目录以外的东西")
    return full


# ============================================================
#  multipart/form-data 解析
#  /write_file 接口浏览器用 multipart 传文件， cgi 模块在新版 Python 里废弃了，
#  所以自己手撸一个简易解析器，够用就行。
# ============================================================


def _parse_multipart(body, content_type):
    """解析 multipart/form-data。
    返回 {字段名: {"filename": str|None, "data": bytes}}。"""
    # 先从 Content-Type 里把 boundary 提出来
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part[len("boundary="):]
            if len(boundary) >= 2 and boundary[0] == '"' and boundary[-1] == '"':
                boundary = boundary[1:-1]
            break
    if not boundary:
        return {}

    b_delim = b"--" + boundary.encode("ascii")
    # 用 \r\n--boundary 切分各个字段（注意第一段前面带个 --boundary）
    sep = b"\r\n" + b_delim
    segments = body.split(sep)
    result = {}
    for idx, seg in enumerate(segments):
        if idx == 0:
            # 第一段，去掉开头的 --boundary\r\n
            if seg.startswith(b_delim + b"\r\n"):
                seg = seg[len(b_delim) + 2:]
            elif seg.startswith(b_delim):
                seg = seg[len(b_delim):]
                if seg.startswith(b"\r\n"):
                    seg = seg[2:]
        else:
            # 后续段，去掉开头那个 \r\n
            if seg.startswith(b"\r\n"):
                seg = seg[2:]
        # 结束标记 --boundary-- 后面那段以 -- 开头，跳过
        if seg.startswith(b"--"):
            continue
        # 分离头部和正文
        if b"\r\n\r\n" not in seg:
            continue
        header_block, content = seg.split(b"\r\n\r\n", 1)
        name = None
        filename = None
        for line in header_block.split(b"\r\n"):
            ls = line.decode("utf-8", "ignore")
            low = ls.lower()
            if low.startswith("content-disposition"):
                m = re.search(r'name="([^"]*)"', ls)
                if m:
                    name = m.group(1)
                m2 = re.search(r'filename="([^"]*)"', ls)
                if m2:
                    filename = m2.group(1)
        if name is None:
            continue
        result[name] = {"filename": filename, "data": content}
    return result


# ============================================================
#  HTTP 请求处理
# ============================================================


class _Handler(BaseHTTPRequestHandler):
    # 关掉默认的 stderr 日志，--noconsole 下 stderr 可能是 None 会报错
    def log_message(self, fmt, *args):
        _log(self.address_string() + " " + (fmt % args))

    # ---------- CORS ----------
    def _cors_headers(self):
        # HTTPS 网页要调 http://localhost，必须带 CORS 头，否则浏览器拦
        origin = self.headers.get("Origin", "")
        if not origin or not _is_allowed_origin(origin):
            return
        self.send_header("Access-Control-Allow-Origin", _normalize_origin(origin))
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _reject_disallowed_origin(self):
        origin = self.headers.get("Origin", "")
        if _is_allowed_origin(origin):
            return False
        _log("拒绝跨源请求: " + str(origin))
        self._send_json({"ok": False, "error": "origin not allowed"}, 403)
        return True

    def do_OPTIONS(self):
        if self._reject_disallowed_origin():
            return
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ---------- 通用响应 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, content, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self._cors_headers()
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        """按 Content-Length 读请求体。"""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 0:
            return self.rfile.read(length)
        return b""

    # ---------- GET ----------
    def do_GET(self):
        try:
            if self._reject_disallowed_origin():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path == "/health":
                self._send_json({"ok": True, "status": "online"})
            elif path == "/config":
                cfg = _load_config()
                self._send_json({"ok": True, "project_path": cfg.get("project_path", "")})
            elif path == "/default_path":
                self._handle_default_path()
            elif path == "/read":
                self._handle_read(qs)
            elif path == "/list":
                self._handle_list(qs)
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            _log("GET 错误: " + str(e))
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ---------- POST ----------
    def do_POST(self):
        try:
            if self._reject_disallowed_origin():
                return
            path = urlparse(self.path).path
            if path == "/config":
                body = json.loads(self._read_body().decode("utf-8"))
                _save_config({"project_path": body.get("project_path", "")})
                self._send_json({"ok": True})
            elif path == "/write":
                self._handle_write()
            elif path == "/write_file":
                self._handle_write_file()
            elif path == "/mkdir":
                self._handle_mkdir()
            elif path == "/delete":
                self._handle_delete()
            elif path == "/upload_detection":
                self._handle_upload_detection()
            elif path == "/upload_training":
                self._handle_upload_training()
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            _log("POST 错误: " + str(e))
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ---------- 各接口实现 ----------
    def _handle_default_path(self):
        """返回系统下载目录下的 ColorChase 文件夹路径，自动创建。
        前端首次配置时调这个拿默认地址，不用用户手填。"""
        home = os.path.expanduser("~")
        # 常见的下载目录名，Windows 中文系统可能是"下载"
        candidates = [
            os.path.join(home, "Downloads"),
            os.path.join(home, "下载"),
        ]
        downloads = ""
        for c in candidates:
            if os.path.isdir(c):
                downloads = c
                break
        if not downloads:
            # Downloads 目录不存在，用 home 兜底
            downloads = home
        default_path = os.path.join(downloads, "ColorChase")
        try:
            os.makedirs(default_path, exist_ok=True)
        except Exception as e:
            _log("创建默认目录失败: " + str(e))
        self._send_json({"ok": True, "path": default_path})

    def _handle_read(self, qs):
        """读文件，返回二进制流。"""
        rel = qs.get("path", [""])[0]
        project_id = qs.get("project_id", [""])[0]
        project_name = qs.get("project_name", [""])[0]
        full = _resolve_path(project_id, rel, project_name)
        if not os.path.isfile(full):
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        with open(full, "rb") as f:
            content = f.read()
        self._send_bytes(content)

    def _handle_list(self, qs):
        """列目录，返回 files 和 dirs 两个列表。"""
        rel = qs.get("path", [""])[0]
        project_id = qs.get("project_id", [""])[0]
        project_name = qs.get("project_name", [""])[0]
        full = _resolve_path(project_id, rel, project_name)
        files, dirs = [], []
        if os.path.isdir(full):
            for name in os.listdir(full):
                p = os.path.join(full, name)
                (dirs if os.path.isdir(p) else files).append(name)
        self._send_json({"ok": True, "files": files, "dirs": dirs})

    def _handle_write(self):
        """写文件，内容是 base64。"""
        body = json.loads(self._read_body().decode("utf-8"))
        rel = body.get("path", "")
        data_b64 = body.get("data", "")
        project_id = body.get("project_id", "")
        project_name = body.get("project_name", "")
        full = _resolve_path(project_id, rel, project_name)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        content = base64.b64decode(data_b64)
        with open(full, "wb") as f:
            f.write(content)
        self._send_json({"ok": True, "path": full})

    def _handle_write_file(self):
        """写文件，multipart form 上传。"""
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        fields = _parse_multipart(body, ctype)
        if "file" not in fields:
            self._send_json({"ok": False, "error": "缺少 file 字段"}, 400)
            return
        rel = fields.get("path", {}).get("data", b"").decode("utf-8", "ignore")
        project_id = fields.get("project_id", {}).get("data", b"").decode("utf-8", "ignore")
        project_name = fields.get("project_name", {}).get("data", b"").decode("utf-8", "ignore")
        full = _resolve_path(project_id, rel, project_name)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as f:
            f.write(fields["file"]["data"])
        self._send_json({"ok": True, "path": full})

    def _handle_mkdir(self):
        """建目录。"""
        body = json.loads(self._read_body().decode("utf-8"))
        full = _resolve_path(body.get("project_id", ""), body.get("path", ""), body.get("project_name", ""))
        os.makedirs(full, exist_ok=True)
        self._send_json({"ok": True, "path": full})

    def _handle_delete(self):
        """删文件或目录。"""
        body = json.loads(self._read_body().decode("utf-8"))
        full = _resolve_path(body.get("project_id", ""), body.get("path", ""), body.get("project_name", ""))
        if os.path.isfile(full):
            os.remove(full)
        elif os.path.isdir(full):
            shutil.rmtree(full)
        else:
            self._send_json({"ok": False, "error": "目标不存在"}, 404)
            return
        self._send_json({"ok": True, "path": full})

    def _handle_upload_detection(self):
        """把本地原图推到远端检测库。"""
        body = json.loads(self._read_body().decode("utf-8"))
        code, text = _i0x1(
            body["file_path"],
            body["file_uuid"],
            body.get("token", ""),
        )
        self._send_json({"ok": code < 400, "status": code, "response": text})

    def _handle_upload_training(self):
        """把本地 target+reference+result+meta 推到远端训练库。
        视频导出不走这里（前端不会调这个接口）。"""
        body = json.loads(self._read_body().decode("utf-8"))
        code, text = _i0x2(
            body["target_path"],
            body.get("reference_path"),
            body["result_path"],
            body.get("meta", {}),
            body["sample_uuid"],
            body.get("is_video", False),
            body.get("token", ""),
        )
        self._send_json({"ok": code < 400, "status": code, "response": text})


# ============================================================
#  启动服务
# ============================================================


def main():
    # 用 ThreadingHTTPServer，每个请求一个线程，避免大文件读写阻塞别的请求
    server = ThreadingHTTPServer(("127.0.0.1", 9123), _Handler)
    server.daemon_threads = True
    _log("agent 已启动，监听 127.0.0.1:9123")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("agent 收到退出信号，停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
