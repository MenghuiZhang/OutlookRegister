"""
OutlookRegister 网页控制台

功能:
  - 登录认证: 会话 cookie + 账号文件 (data/ui_auth.json), 默认 admin/admin, IP 防爆破锁定
  - 配置编辑: 表单化修改 config.json (保存前自动备份为 config.json.bak)
  - 任务控制: 启动 / 停止注册任务, 实时统计(已提交/成功/失败)
  - 实时日志: 捕获 print 输出, 通过 WebSocket 推送到页面
  - 结果查看: 分文件表格化展示 Results/*.txt

启动: python web_console.py [端口]   默认 http://127.0.0.1:8788
依赖: pip install fastapi uvicorn
"""
import argparse
import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from urllib.parse import quote as _urlquote

import requests

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
    import uvicorn
except ImportError:
    print("缺少依赖，请先执行: pip install fastapi uvicorn")
    sys.exit(1)

from proxy_pool import (
    ProxyConfigError,
    get_proxy_pool,
    mask_proxy,
    parse_proxy_lines,
)
from proxy_probe import probe_saved_proxy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
RESULTS_DIR = os.path.join(BASE_DIR, "Results")
INDEX_PATH = os.path.join(BASE_DIR, "web", "index.html")

proxy_pool = get_proxy_pool(BASE_DIR)

os.makedirs(RESULTS_DIR, exist_ok=True)

# 已知结果文件的表头，未识别的文件使用通用表头
RESULT_HEADERS = {
    "secured_accounts.txt": ["邮箱", "密码", "辅助邮箱"],
    "secured_no_token.txt": ["邮箱", "密码", "辅助邮箱"],
    "secured_tokens.txt": ["邮箱", "密码", "refresh_token", "access_token", "过期时间", "辅助邮箱"],
    "unbound_accounts.txt": ["邮箱", "密码"],
    "recovery_pool.txt": ["邮箱", "密码", "refresh_token", "access_token", "过期时间"],
}

# config.json 字段提示 (title 悬浮显示)
CONFIG_HINTS = {
    "email_suffix": "注册域名后缀: @outlook.com 或 @hotmail.com",
    "proxy": "代理地址, 留空直连 (浏览器不会走系统代理, 需显式指定)",
    "bot_protection_wait": "反机器人检测等待秒数",
    "max_captcha_retries": "验证码最大重试次数",
    "concurrent_flows": "并发浏览器数量",
    "max_tasks": "总注册任务数",
    "enable_oauth2": "是否注册后走 OAuth2 拿 token",
    "client_id": "Azure 应用 client id, 形如 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "redirect_url": "OAuth2 回调地址, 通常为 http://localhost:8000",
    "Scopes": "OAuth2 权限范围 (JSON 数组)",
    "headless": "无头模式, 注册任务建议 false (无头易被检测)",
    "geoip": "时区/语言/经纬度/分辨率自动跟随代理 IP",
    "humanize": "鼠标移动拟人轨迹",
    "locale": "锁定浏览器语言 (注册流程依赖中文页面, 保持 zh-CN; 留空则跟随出口 IP)",
    "os": "指纹 OS (macOS 上默认 macos, Windows 身份在 Mac 有头模式会中文乱码)",
    "version": "指定 camoufox 运行时版本 (如 158.0-beta.40), 留空用 pip 包锁定版本",
    "keep_browser": "注册结束后保留浏览器窗口, 便于人工检查结果",
    "block_images": "禁止加载图片省流量 (camoufox 官方提示易被 WAF 检测, 默认关闭)",
    "block_webrtc": "屏蔽 WebRTC (geoip 已伪装 IP 时无需开启)",
}


# ============ 日志广播 ============
_loop = None  # 主事件循环, lifespan 中赋值


class LogBroadcaster:
    """线程安全: 缓存最近日志 + 维护 WebSocket 客户端队列"""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = set()
        self._buffer = []
        self._max_buffer = 2000

    def connect(self, queue):
        with self._lock:
            self._clients.add(queue)

    def disconnect(self, queue):
        with self._lock:
            self._clients.discard(queue)

    def append_buffer(self, msg):
        with self._lock:
            self._buffer.append(msg)
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[-self._max_buffer // 2:]

    def replay(self, n=500):
        with self._lock:
            return list(self._buffer[-n:])

    def fanout(self, msg):
        with self._lock:
            targets = list(self._clients)
        for q in targets:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass
            except Exception:
                pass


broadcaster = LogBroadcaster()


def _deliver(msg):
    """在事件循环线程内执行: 落缓冲 + 分发"""
    if isinstance(msg, str):
        msg = {"type": "log", "text": msg}
    broadcaster.append_buffer(msg)
    broadcaster.fanout(msg)


def broadcast(msg):
    """任意线程调用, 把日志/状态推给所有 WebSocket 客户端"""
    if _loop is None:
        return
    try:
        _loop.call_soon_threadsafe(_deliver, msg)
    except RuntimeError:
        pass  # 服务已关闭


class TeeStream:
    """接管 sys.stdout: 终端照常输出, 整行日志同时推给 WebSocket"""

    def __init__(self, original):
        self.original = original
        self._partial = ""
        self._lock = threading.Lock()

    def write(self, text):
        self.original.write(text)
        try:
            with self._lock:
                self._partial += text
                while "\n" in self._partial:
                    line, self._partial = self._partial.split("\n", 1)
                    line = line.rstrip()
                    if line:
                        broadcast(line)
                        self.original.flush()  # 立即落盘, 便于后台/重定向时查看日志
        except Exception:
            pass
        return len(text)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.original, name)


_original_stdout = sys.stdout


def install_stdout_capture():
    if not isinstance(sys.stdout, TeeStream):
        sys.stdout = TeeStream(_original_stdout)


# ============ 任务管理 ============
def build_controller():
    """固定使用 camoufox 控制器"""
    from controllers.camoufox_controller import CamoufoxController
    return CamoufoxController()


class TaskManager:
    """
    网页版的注册任务调度, 逻辑对齐 main.run_concurrent_flows, 增加了:
      - stop_event: 停止后不再提交新任务
      - 停止时强制关闭所有浏览器, 让在途任务快速失败退出
      - 每次启动创建全新的 controller / 线程池, 保证配置修改生效、状态不残留
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"  # idle / running / stopping
        self.stop_event = threading.Event()
        self.thread = None
        self.stats = {"submitted": 0, "success": 0, "failed": 0, "max_tasks": 0, "concurrent_flows": 0}

    def status_dict(self):
        with self.lock:
            return {"state": self.state, **self.stats}

    def push_status(self):
        broadcast({"type": "status", **self.status_dict()})

    def start(self):
        with self.lock:
            if self.state != "idle":
                return False, f"当前状态为 [{self.state}]，无法启动"

            # 读取配置
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:
                return False, f"读取 config.json 失败: {e}"

            max_tasks = int(cfg.get("max_tasks") or 0)
            concurrent_flows = max(1, int(cfg.get("concurrent_flows") or 1))
            if max_tasks <= 0:
                return False, "config.json 中 max_tasks 需大于 0"

            # 导入注册流程 (main.py 顶层会导入全部控制器, 缺依赖时给出提示)
            try:
                from main import process_single_flow
            except Exception as e:
                return False, f"导入注册流程失败，请检查依赖已安装: {e}"

            try:
                controller = build_controller()
            except Exception as e:
                return False, f"初始化浏览器控制器失败: {e}"

            self.stats = {
                "submitted": 0, "success": 0, "failed": 0,
                "max_tasks": max_tasks, "concurrent_flows": concurrent_flows,
            }
            self.stop_event.clear()
            self.state = "running"
            self.thread = threading.Thread(
                target=self._run,
                args=(controller, process_single_flow, concurrent_flows, max_tasks),
                daemon=True,
            )
            self.thread.start()

        # 代理池后台预检 (注册期间还会周期复检冷却恢复的节点)
        pool_stats = proxy_pool.summary()
        if pool_stats["total"] > 0:
            checked = _start_proxy_pool_health_checks()
            print(f"[Console] 代理池后台预检已启动: {checked} 条 (共 {pool_stats['total']} 条)")
        else:
            print("[Console] 代理池为空，本批注册使用 config 代理/直连")
        return True, "注册任务已启动"

    def stop(self):
        with self.lock:
            if self.state != "running":
                return False, "当前没有运行中的任务"
            self.state = "stopping"
            self.stop_event.set()
        print("[Console] 收到停止请求，正在停止任务...")
        self.push_status()
        return True, "停止中：不再提交新任务，并关闭浏览器中断在途任务"

    def _run(self, controller, process_fn, concurrent_flows, max_tasks):
        from main import run_single_flow_with_pool

        submitted = 0
        forced_closed = False
        executor = ThreadPoolExecutor(max_workers=concurrent_flows)
        running = set()

        print(f"[Console] 任务启动：并发 {concurrent_flows}，总任务 {max_tasks}")
        self.push_status()

        try:
            while running or (not self.stop_event.is_set() and submitted < max_tasks):
                # 停止请求: 强制关闭所有浏览器, 在途任务会因浏览器关闭而快速失败
                if self.stop_event.is_set() and not forced_closed and running:
                    forced_closed = True
                    print("[Console] 正在强制关闭所有浏览器以中断在途任务...")
                    try:
                        controller.clean_up(type="all_browser")
                    except Exception as e:
                        print(f"[Console] 强制关闭浏览器异常: {e}")

                # 回收已完成任务
                for f in [f for f in running if f.done()]:
                    running.discard(f)
                    try:
                        ok = f.result()
                    except Exception as e:
                        ok = False
                        print(f"[Console] 任务异常退出: {e}")
                    with self.lock:
                        self.stats["success" if ok else "failed"] += 1
                    self.push_status()

                # 提交新任务 (任务内先按调度策略从代理池租节点, 默认顺序轮询)
                while (not self.stop_event.is_set()
                       and submitted < max_tasks
                       and len(running) < concurrent_flows):
                    running.add(executor.submit(
                        run_single_flow_with_pool, controller, process_fn, f"W{submitted + 1}"
                    ))
                    submitted += 1
                    with self.lock:
                        self.stats["submitted"] = submitted
                    self.push_status()

                time.sleep(0.3)
        except Exception as e:
            print(f"[Console] 调度线程异常: {e}")
        finally:
            executor.shutdown(wait=True)
            try:
                controller.clean_up(type="all_browser")
            except Exception:
                pass
            s = self.status_dict()
            print(f"[Console] 任务结束：成功 {s['success']}，失败 {s['failed']}，共 {s['submitted']}")
            with self.lock:
                self.state = "idle"
            self.push_status()


task_manager = TaskManager()


# ============ FastAPI ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    install_stdout_capture()
    yield


app = FastAPI(title="OutlookRegister Console", lifespan=lifespan)


# ============ 登录认证（移植自 grok 工作台）============
# 会话 cookie (HMAC 自签) + 账号文件 data/ui_auth.json；默认 admin/admin，
# 可用环境变量 UI_USERNAME/UI_PASSWORD 覆盖。改密后密钥轮换，所有旧会话失效。
UI_DEFAULT_USERNAME = "admin"
UI_DEFAULT_PASSWORD = "admin"
UI_SESSION_LIFETIME_DAYS = 7
UI_LOGIN_MAX_FAILS = 5
UI_LOGIN_LOCK_SECONDS = 15 * 60
UI_PASSWORD_MIN_LENGTH = 8
SESSION_COOKIE = "or_session"

UI_AUTH_PATH = os.path.join(BASE_DIR, "data", "ui_auth.json")
_SECRET_KEY_PATH = os.path.join(BASE_DIR, "data", ".ui_secret_key")
LOGIN_PAGE_PATH = os.path.join(BASE_DIR, "web", "login.html")

# 登录白名单：登录登出永远放行
UI_PUBLIC_PATHS = frozenset({
    "/login",
    "/logout",
    "/api/auth/login",
    "/api/auth/meta",
})


def _ui_credentials() -> tuple[str, str]:
    """当前登录账号：环境变量优先，其次 data/ui_auth.json，缺省 admin/admin。

    密码支持 "sha256:<hex>" 哈希格式（网页改密后落盘的格式）。
    """
    username = (os.getenv("UI_USERNAME") or "").strip()
    raw = (os.getenv("UI_PASSWORD") or "").strip()
    if username and raw:
        return username, raw
    try:
        with open(UI_AUTH_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return (
            str(saved.get("username") or UI_DEFAULT_USERNAME),
            str(saved.get("password_hash") or UI_DEFAULT_PASSWORD),
        )
    except Exception:
        return UI_DEFAULT_USERNAME, UI_DEFAULT_PASSWORD


def _load_or_create_secret_key() -> str:
    """会话签名密钥 = 随机密钥(持久化) + 当前密码哈希派生值。

    绑定密码：改密后旧 cookie 全部失效（包括未走 /logout 的会话），
    防止“改密踢不掉已泄露 cookie”的问题。
    """
    random_part = ""
    try:
        if os.path.exists(_SECRET_KEY_PATH):
            with open(_SECRET_KEY_PATH, "r", encoding="utf-8") as f:
                random_part = f.read().strip()
        if not random_part:
            random_part = uuid.uuid4().hex + uuid.uuid4().hex
            os.makedirs(os.path.dirname(_SECRET_KEY_PATH), exist_ok=True)
            with open(_SECRET_KEY_PATH, "w", encoding="utf-8") as f:
                f.write(random_part)
            try:
                os.chmod(_SECRET_KEY_PATH, 0o600)
            except OSError:
                pass
    except Exception:
        # data/ 不可写时退化为进程内随机密钥（重启即全员下线，但不影响功能）
        random_part = uuid.uuid4().hex + uuid.uuid4().hex
    _, stored = _ui_credentials()
    return hashlib.sha256(f"{random_part}::{stored}".encode("utf-8")).hexdigest()


_secret_key_cache = ""


def _secret_key() -> str:
    global _secret_key_cache
    if not _secret_key_cache:
        _secret_key_cache = _load_or_create_secret_key()
    return _secret_key_cache


def _sign_session(payload: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8").rstrip("=")
    sig = hmac.new(_secret_key().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _read_session(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        expect = hmac.new(_secret_key().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if float(payload.get("exp") or 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _hash_ui_password(plain: str) -> str:
    return "sha256:" + hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _ui_password_matches(supplied: str) -> bool:
    _, stored = _ui_credentials()
    if stored.startswith("sha256:"):
        got = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        return hmac.compare_digest(got, stored[len("sha256:"):])
    return hmac.compare_digest(supplied, stored)


def _is_default_credentials() -> bool:
    username, stored = _ui_credentials()
    return username == UI_DEFAULT_USERNAME and stored == UI_DEFAULT_PASSWORD


# 简单内存级防爆破：IP -> {fails, locked_until, last}
_login_failures: dict[str, dict] = {}
_login_guard_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "unknown")


def _login_locked_remaining(ip: str) -> int:
    with _login_guard_lock:
        info = _login_failures.get(ip)
        if not info:
            return 0
        remain = int(info.get("locked_until", 0) - time.time())
        if remain <= 0:
            _login_failures.pop(ip, None)
            return 0
        return remain


def _record_login_fail(ip: str) -> None:
    with _login_guard_lock:
        now = time.time()
        info = _login_failures.setdefault(ip, {"fails": 0, "locked_until": 0, "last": 0})
        # 失败计数只在锁定窗口内累积，超窗自动重新计数
        if now - info.get("last", 0) > UI_LOGIN_LOCK_SECONDS:
            info["fails"] = 0
        info["fails"] += 1
        info["last"] = now
        if info["fails"] >= UI_LOGIN_MAX_FAILS:
            info["locked_until"] = now + UI_LOGIN_LOCK_SECONDS
            info["fails"] = 0


def _clear_login_fails(ip: str) -> None:
    with _login_guard_lock:
        _login_failures.pop(ip, None)


def _sanitize_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


@app.middleware("http")
async def ui_auth_guard(request: Request, call_next):
    """全局登录拦截：白名单放行；API 返回 401 JSON；页面 302 到登录页。"""
    path = request.url.path
    if path in UI_PUBLIC_PATHS:
        return await call_next(request)
    payload = _read_session(request.cookies.get(SESSION_COOKIE) or "")
    if payload and payload.get("ui_authed"):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "message": "登录已过期，请重新登录"}, status_code=401)
    return RedirectResponse(f"/login?next={_urlquote(path)}", status_code=302)


@app.get("/login")
def login_page(request: Request):
    payload = _read_session(request.cookies.get(SESSION_COOKIE) or "")
    if payload and payload.get("ui_authed"):
        return RedirectResponse("/", status_code=302)
    return FileResponse(LOGIN_PAGE_PATH)


@app.post("/api/auth/login")
def auth_login(request: Request, payload: dict):
    body = payload or {}
    next_url = _sanitize_next(str(body.get("next") or "/"))
    ip = _client_ip(request)
    locked = _login_locked_remaining(ip)
    if locked > 0:
        return JSONResponse({
            "ok": False,
            "locked": locked,
            "message": f"失败次数过多，已锁定，请 {locked // 60} 分钟后重试",
        }, status_code=429)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    expected_user, _ = _ui_credentials()
    if username != expected_user or not _ui_password_matches(password):
        _record_login_fail(ip)
        print(f"[Auth] 登录失败：用户名或密码错误（IP {ip}）")
        return JSONResponse({"ok": False, "message": "用户名或密码错误"}, status_code=401)
    _clear_login_fails(ip)
    token = _sign_session({
        "ui_authed": True,
        "ui_user": username,
        "exp": time.time() + UI_SESSION_LIFETIME_DAYS * 86400,
    })
    resp = JSONResponse({"ok": True, "next": next_url})
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=UI_SESSION_LIFETIME_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
    )
    print(f"[Auth] 用户 {username} 登录成功（IP {ip}）")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/meta")
def auth_meta():
    """登录页查询是否仍为默认账号（公开接口，不含敏感信息）。"""
    return {"ok": True, "is_default_credentials": _is_default_credentials()}


@app.get("/api/ui/session")
def ui_session(request: Request):
    """前端查询当前登录态与默认密码警告。"""
    payload = _read_session(request.cookies.get(SESSION_COOKIE) or "")
    if not payload or not payload.get("ui_authed"):
        return JSONResponse({"ok": False, "authed": False}, status_code=401)
    return {
        "ok": True,
        "authed": True,
        "username": payload.get("ui_user", ""),
        "is_default_password": _is_default_credentials(),
    }


@app.post("/api/ui/change-password")
def ui_change_password(request: Request, payload: dict):
    body = payload or {}
    old_password = str(body.get("old_password") or "")
    new_password = str(body.get("new_password") or "")
    username = str(body.get("username") or "").strip() or _ui_credentials()[0]
    if not _ui_password_matches(old_password):
        return JSONResponse({"ok": False, "message": "当前密码不正确"}, status_code=400)
    if len(new_password) < UI_PASSWORD_MIN_LENGTH:
        return JSONResponse({"ok": False, "message": f"新密码至少 {UI_PASSWORD_MIN_LENGTH} 位"}, status_code=400)
    if new_password == old_password:
        return JSONResponse({"ok": False, "message": "新密码不能与当前密码相同"}, status_code=400)
    # 落盘：哈希存储，明文密码不保留
    data = {}
    try:
        if os.path.exists(UI_AUTH_PATH):
            with open(UI_AUTH_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = {}
    data["username"] = username
    data["password_hash"] = _hash_ui_password(new_password)
    os.makedirs(os.path.dirname(UI_AUTH_PATH), exist_ok=True)
    with open(UI_AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(UI_AUTH_PATH, 0o600)
    except OSError:
        pass
    # 轮换会话签名密钥：所有旧 cookie（含本会话）立即失效，需用新密码重新登录
    global _secret_key_cache
    _secret_key_cache = ""
    _secret_key()
    print("[Auth] 登录密码已修改，所有会话已失效")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(INDEX_PATH)


# ---- 配置 ----
@app.get("/api/config")
def get_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(500, f"读取 config.json 失败: {e}")


@app.post("/api/config")
def save_config(payload: dict):
    if not isinstance(payload, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                with open(CONFIG_PATH + ".bak", "w", encoding="utf-8") as b:
                    b.write(f.read())
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        return {"ok": True, "message": "配置已保存 (原配置备份为 config.json.bak)"}
    except Exception as e:
        raise HTTPException(500, f"保存 config.json 失败: {e}")


# ---- 任务 ----
@app.get("/api/tasks/status")
def tasks_status():
    return task_manager.status_dict()


@app.post("/api/tasks/start")
def tasks_start():
    ok, message = task_manager.start()
    if not ok:
        print(f"[Console] 启动失败: {message}")
    return {"ok": ok, "message": message}


@app.post("/api/tasks/stop")
def tasks_stop():
    ok, message = task_manager.stop()
    return {"ok": ok, "message": message}


# ---- 结果 ----
def _parse_result_file(path):
    """把结果文件的行解析为二维数组"""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if "----" in line:
                # 新分隔符: 密码字符集不含连字符, 不会与密码内容冲突
                head, _, rest = line.partition("----")
                if ": " in head or head.endswith(":"):
                    email = head.rsplit(":", 1)[0]
                    rows.append([email] + rest.split("----"))
                else:
                    rows.append(line.split("----"))
            elif "---" in line:
                head, _, rest = line.partition("---")
                if ": " in head or head.endswith(":"):
                    email = head.rsplit(":", 1)[0]
                    rows.append([email] + rest.split("---"))
                else:
                    rows.append(line.split("---"))
            elif ": " in line:
                email, pwd = line.split(": ", 1)
                rows.append([email, pwd])
            else:
                rows.append([line])
    return rows


@app.get("/api/results")
def list_results():
    files = []
    if os.path.isdir(RESULTS_DIR):
        for name in sorted(os.listdir(RESULTS_DIR)):
            path = os.path.join(RESULTS_DIR, name)
            if name.endswith(".txt") and os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        count = sum(1 for l in f if l.strip())
                except Exception:
                    count = 0
                files.append({"name": name, "count": count, "exists": True})
    return files


@app.get("/api/results/{name}")
def get_result(name: str):
    path = os.path.join(RESULTS_DIR, os.path.basename(name))
    if not path.endswith(".txt") or not os.path.isfile(path):
        raise HTTPException(404, f"结果文件不存在: {name}")
    rows = _parse_result_file(path)
    headers = RESULT_HEADERS.get(os.path.basename(name))
    if not headers or (rows and max(len(r) for r in rows) > len(headers)):
        width = max((len(r) for r in rows), default=1)
        headers = [f"字段{i + 1}" for i in range(width)]
    return {"name": name, "headers": headers, "rows": rows, "count": len(rows)}


# ---- 运行截图 (Results/screenshots) ----
@app.get("/api/screenshots")
def list_screenshots():
    """按任务目录列出截图 (最新在前)"""
    root = os.path.join(RESULTS_DIR, "screenshots")
    tasks = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if name.startswith(".") or not os.path.isdir(d):
                continue
            files = []
            for fn in sorted(os.listdir(d)):
                fp = os.path.join(d, fn)
                if fn.lower().endswith(".png") and os.path.isfile(fp):
                    try:
                        files.append({"name": fn, "size": os.path.getsize(fp)})
                    except OSError:
                        pass
            if files:
                try:
                    mtime = os.path.getmtime(d)
                except OSError:
                    mtime = 0
                tasks.append({"dir": name, "count": len(files), "mtime": mtime, "files": files})
    tasks.sort(key=lambda t: t["mtime"], reverse=True)
    return tasks


@app.get("/api/screenshots/{dir_name}/{file_name}")
def get_screenshot(dir_name: str, file_name: str):
    root = os.path.join(RESULTS_DIR, "screenshots")
    d = os.path.join(root, os.path.basename(dir_name))
    path = os.path.join(d, os.path.basename(file_name))
    if not path.lower().endswith(".png") or not os.path.isfile(path):
        raise HTTPException(404, "截图不存在")
    return FileResponse(path)


# ---- 邮箱服务 (cf_tempmail) ----
@app.post("/api/email/test")
def email_test():
    """真实连通测试：创建一个临时邮箱地址（按当前配置的域名策略），成功后删除。

    域名策略与注册流程一致：
      - domains 配置了 → 从中随机选一个
      - 服务端自动选域 → 请求不带 domain 参数
      - 启用随机子域 → 附加 enableRandomSubdomain=true
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return _proxy_error(f"读取 config.json 失败: {e}")

    cf = cfg.get("cf_tempmail") or {}
    base_url = (cf.get("base_url") or "").rstrip("/")
    if not base_url:
        return _proxy_error("请先在配置里填写 Worker 地址 (base_url)")

    admin_password = cf.get("admin_password") or ""
    site_password = cf.get("site_password") or ""
    headers = {"Content-Type": "application/json"}
    if site_password:
        headers["x-custom-auth"] = site_password

    settings_error = ""
    try:
        resp = requests.get(f"{base_url}/open_api/settings", headers=headers, timeout=10)
        settings_ok = resp.status_code == 200
        if not settings_ok:
            settings_error = f"HTTP {resp.status_code}"
    except Exception as e:
        settings_ok = False
        settings_error = str(e)[:120]

    mode = "admin" if admin_password else "user"
    if mode == "admin":
        headers["x-admin-auth"] = admin_password
        create_path = "/admin/new_address"
    else:
        create_path = "/api/new_address"

    domains = [d.strip() for d in (cf.get("domains") or "").split(",") if d.strip()]
    auto_domain = bool(cf.get("auto_domain"))
    random_subdomain = bool(cf.get("random_subdomain"))

    body = {"name": f"probe-{uuid.uuid4().hex[:10]}"}
    if not auto_domain and domains:
        body["domain"] = random.choice(domains)
    if random_subdomain:
        body["enableRandomSubdomain"] = True

    created_email = ""
    errors = []
    for _ in range(3):
        try:
            resp = requests.post(f"{base_url}{create_path}", headers=headers, json=body, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                created_email = data.get("address") or data.get("email") or ""
                if created_email:
                    break
            errors.append(f"HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            errors.append(str(e)[:120])
        body["name"] = f"probe-{uuid.uuid4().hex[:10]}"

    if not created_email:
        return JSONResponse({
            "ok": False,
            "message": f"创建邮箱失败 ({mode}) · 设置页 {'OK' if settings_ok else settings_error} · " + " | ".join(errors[-2:]),
        }, status_code=502)

    # 管理员模式: 删除探测地址, 不留垃圾
    if mode == "admin":
        try:
            requests.delete(
                f"{base_url}/admin/delete_address/{created_email}",
                headers=headers, timeout=10,
            )
        except Exception:
            pass

    policy_parts = []
    if auto_domain or not domains:
        policy_parts.append("服务端自动选域")
    else:
        policy_parts.append(f"域名池 {len(domains)} 个")
    if random_subdomain:
        policy_parts.append("随机子域")

    message = f"连接正常 ({mode}) · {' · '.join(policy_parts)} · 试建 {created_email}"
    return {"ok": True, "message": message, "email": created_email, "mode": mode}


# ---- 代理池 ----
def _proxy_error(message: str, status: int = 400, **extra):
    return JSONResponse({"ok": False, "message": message, **extra}, status_code=status)


@app.get("/api/proxies")
def proxy_pool_list(page: int = 1, per_page: int = 50, q: str = "", status: str = ""):
    try:
        data = proxy_pool.list_public(page=page, per_page=per_page, query=q, status=status)
        return {"ok": True, **data}
    except Exception as exc:
        return _proxy_error(str(exc))


@app.patch("/api/proxies/settings")
def proxy_pool_settings(payload: dict):
    try:
        settings = proxy_pool.update_settings(payload or {})
        return {"ok": True, "settings": settings}
    except (ProxyConfigError, TypeError, ValueError) as exc:
        return _proxy_error(str(exc))


@app.post("/api/proxies/preview")
def proxy_pool_preview(payload: dict):
    body = payload or {}
    text = str(body.get("text") or "")
    mode = str(body.get("mode") or "append")
    try:
        rows, errors, duplicates = parse_proxy_lines(
            text,
            default_scheme=proxy_pool.settings().get("default_scheme", "http"),
        )
    except (ProxyConfigError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "message": str(exc), "errors": []}, status_code=400
        )
    return JSONResponse(
        {
            "ok": not errors,
            "mode": mode,
            "valid": len(rows),
            "duplicates": duplicates,
            "errors": errors[:200],
            "preview": [
                {"line": idx + 1, "proxy": mask_proxy(row["spec"])}
                for idx, row in enumerate(rows[:20])
            ],
        },
        status_code=200 if not errors else 400,
    )


@app.post("/api/proxies/import")
def proxy_pool_import(payload: dict):
    body = payload or {}
    text = str(body.get("text") or "")
    mode = str(body.get("mode") or "append")
    try:
        result = proxy_pool.import_text(text, mode=mode)
    except (ProxyConfigError, TypeError, ValueError) as exc:
        return _proxy_error(str(exc))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    added_ids = list(result.pop("added_ids", []))
    if added_ids:
        _start_proxy_pool_health_checks(added_ids)
    print(f"[Console] 注册代理池已导入 {result.get('added', 0)} 条")
    return result


@app.post("/api/proxies/delete")
def proxy_pool_delete(payload: dict):
    ids = (payload or {}).get("ids") or []
    if not isinstance(ids, list) or not ids:
        return _proxy_error("请选择要删除的代理")
    result = proxy_pool.delete(ids)
    return {"ok": True, **result}


@app.post("/api/proxies/dedupe")
def proxy_pool_dedupe():
    """全库去重：先按 spec，再按出口 IP，各组保留一条。"""
    result = proxy_pool.dedupe()
    return {"ok": True, **result}


@app.post("/api/proxies/test")
def proxy_pool_test_many(payload: dict):
    ids = (payload or {}).get("ids") or []
    if not isinstance(ids, list) or not ids:
        return _proxy_error("请选择要测试的代理")
    ids = [str(x) for x in ids[:2000]]

    def _one(proxy_id: str) -> dict:
        try:
            result = probe_saved_proxy(proxy_id, timeout=6)
            proxy_pool.set_health(proxy_id, result)
            return {
                "id": proxy_id,
                "ok": bool(result.get("ok")),
                "message": result.get("message") or result.get("error") or "",
            }
        except KeyError:
            return {"id": proxy_id, "ok": False, "message": "代理不存在"}
        except Exception as exc:
            proxy_pool.set_health(proxy_id, {"ok": False, "error": str(exc)})
            return {"id": proxy_id, "ok": False, "message": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        results = list(pool.map(_one, ids))
    passed = sum(1 for x in results if x["ok"])
    return {"ok": True, "all_passed": passed == len(results), "passed": passed, "total": len(results), "results": results}


@app.post("/api/proxies/bulk")
def proxy_pool_bulk(payload: dict):
    body = payload or {}
    ids = [str(x) for x in (body.get("ids") or [])][:2000]
    action = str(body.get("action") or "")
    if not ids:
        return _proxy_error("请选择代理")
    if action not in {"enable", "disable"}:
        return _proxy_error("批量操作无效")
    if action == "disable":
        changed = failed = 0
        errors = []
        for proxy_id in ids:
            try:
                proxy_pool.update_entry(proxy_id, {"enabled": False})
                changed += 1
            except Exception as exc:
                failed += 1
                errors.append({"id": proxy_id, "message": str(exc)})
        return {"ok": True, "all_passed": failed == 0, "changed": changed, "failed": failed, "errors": errors[:100]}

    def _test_and_enable(proxy_id: str) -> dict:
        try:
            result = probe_saved_proxy(proxy_id, timeout=6)
            proxy_pool.set_health(proxy_id, result)
            if not result.get("ok"):
                return {"id": proxy_id, "ok": False, "message": result.get("message") or result.get("error") or "测试失败"}
            proxy_pool.update_entry(proxy_id, {"enabled": True})
            return {"id": proxy_id, "ok": True, "message": "测试通过并已启用"}
        except Exception as exc:
            return {"id": proxy_id, "ok": False, "message": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        results = list(pool.map(_test_and_enable, ids))
    changed = sum(1 for x in results if x["ok"])
    return {
        "ok": True,
        "all_passed": changed == len(results),
        "changed": changed,
        "failed": len(results) - changed,
        "results": results,
    }


@app.get("/api/proxies/export")
def proxy_pool_export(q: str = ""):
    # 导出只用于运维审计，服务端永不读出凭据。分页拉取全量脱敏条目。
    from datetime import datetime, timezone

    items = []
    page = 1
    while True:
        chunk = proxy_pool.list_public(page=page, per_page=200, query=q)
        items.extend(chunk["items"])
        if len(items) >= chunk["total"]:
            break
        page += 1
    return JSONResponse({
        "ok": True,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    })


@app.patch("/api/proxies/{proxy_id}")
def proxy_pool_update_entry(proxy_id: str, payload: dict):
    try:
        item = proxy_pool.update_entry(proxy_id, payload or {})
        return {"ok": True, "item": item}
    except KeyError:
        return _proxy_error("代理不存在", status=404)
    except (ProxyConfigError, TypeError, ValueError) as exc:
        return _proxy_error(str(exc))


@app.post("/api/proxies/{proxy_id}/move")
def proxy_pool_move(proxy_id: str, payload: dict):
    direction = str((payload or {}).get("direction") or "")
    if direction not in ("up", "down"):
        return _proxy_error("direction 必须是 up/down")
    try:
        proxy_pool.move(proxy_id, direction)
        return {"ok": True}
    except KeyError:
        return _proxy_error("代理不存在", status=404)


# ---- 代理池后台健康检查 ----
_proxy_health_lock = threading.Lock()
_proxy_health_stop: threading.Event | None = None
_proxy_health_thread: threading.Thread | None = None


def _stop_proxy_pool_health_checks() -> None:
    global _proxy_health_stop, _proxy_health_thread
    with _proxy_health_lock:
        stop_event = _proxy_health_stop
        thread = _proxy_health_thread
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread is not threading.current_thread() and thread.is_alive():
        # 单条探测最多 ~10s。等在飞的一轮结束，避免下一批重叠。
        thread.join(timeout=15.0)
    with _proxy_health_lock:
        if _proxy_health_thread is thread and (thread is None or not thread.is_alive()):
            _proxy_health_stop = None
            _proxy_health_thread = None


def _start_proxy_pool_health_checks(selected_ids: list[str] | None = None) -> int:
    """后台并行预检代理池；注册期间周期复检冷却恢复的节点。"""
    global _proxy_health_stop, _proxy_health_thread
    _stop_proxy_pool_health_checks()
    with _proxy_health_lock:
        if _proxy_health_thread is not None and _proxy_health_thread.is_alive():
            print("[Console] 上一轮代理健康检查尚未退出，本批不重复启动")
            return 0
    ids = proxy_pool.begin_health_check(selected_ids)
    if not ids:
        return 0
    stop_event = threading.Event()

    def _check(batch: list[str]) -> int:
        def _one(proxy_id: str) -> bool:
            if stop_event.is_set():
                return False
            try:
                result = probe_saved_proxy(proxy_id, timeout=6)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            proxy_pool.set_health(proxy_id, result)
            return bool(result.get("ok"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(batch))) as checks:
            return sum(1 for ok in checks.map(_one, batch) if ok)

    def _run() -> None:
        global _proxy_health_stop, _proxy_health_thread
        try:
            passed = _check(ids)
            if not stop_event.is_set():
                print(f"[Console] 代理池预检完成：{passed}/{len(ids)} 可用")
            # 注册任务运行期间，周期复检冷却恢复的节点
            while not stop_event.is_set() and task_manager.state == "running":
                if stop_event.wait(timeout=5):
                    break
                due = proxy_pool.begin_due_health_checks()
                if due:
                    recovered = _check(due)
                    if not stop_event.is_set():
                        print(f"[Console] 冷却代理复检：{recovered}/{len(due)} 恢复")
        finally:
            with _proxy_health_lock:
                if _proxy_health_thread is threading.current_thread():
                    _proxy_health_stop = None
                    _proxy_health_thread = None

    thread = threading.Thread(target=_run, daemon=True, name="proxy-pool-health")
    with _proxy_health_lock:
        _proxy_health_stop = stop_event
        _proxy_health_thread = thread
    thread.start()
    return len(ids)


# ---- WebSocket 实时日志 ----
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # HTTP 中间件不覆盖 WebSocket, 手动校验会话 cookie
    session = _read_session(websocket.cookies.get(SESSION_COOKIE) or "")
    if not session or not session.get("ui_authed"):
        await websocket.accept()
        await websocket.close(code=4401)  # 前端据此跳转登录页
        return
    await websocket.accept()
    queue = asyncio.Queue(maxsize=1000)
    broadcaster.connect(queue)
    try:
        # 回放最近日志 + 当前任务状态
        for msg in broadcaster.replay():
            await websocket.send_json(msg)
        await websocket.send_json({"type": "status", **task_manager.status_dict()})
        while True:
            msg = await queue.get()
            await websocket.send_json(msg)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        broadcaster.disconnect(queue)


def main():
    parser = argparse.ArgumentParser(description="OutlookRegister 网页控制台")
    parser.add_argument("port", nargs="?", type=int, default=8788, help="监听端口 (默认 8788)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    args = parser.parse_args()

    print(f"[Console] 网页控制台启动: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
