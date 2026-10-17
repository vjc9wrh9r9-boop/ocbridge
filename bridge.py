#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode Free Bridge v2  — 把 OpenCode 的免费模型接进 WorkBuddy

架构:
    WorkBuddy --(OpenAI /v1/chat/completions)--> 本桥 (127.0.0.1:8899)
        --> opencode serve (懒启动, 空闲自退, 127.0.0.1:4999)
        --> OpenCode Zen 免费模型

v2 特性 (参考 OCFerry / OW Bridge 的成熟做法):
    - serve 懒启动 + 空闲自动退出: 不使用时零后端内存占用 (常驻仅本进程 ~30MB)
    - 动态模型发现: 按价格字段筛选免费模型 (不做名字匹配)
    - 登录态检查: 复用本机 opencode 登录 (免费模型要求已登录)
    - JSON 信封工具调用: 模型通过 {"oc_tool_calls": [...]} 表达动作,
      转译为 OpenAI tool_calls; 执行权始终在 WorkBuddy 侧
    - sync 命令: 扫描免费模型 -> 探测可用性 -> 安全合并进 WorkBuddy models.json
      (owner 标记, 只动自己的条目, 备份 + 原子写)
    - 会话缓存 + 增量发送, 伪流式 SSE

命令:
    python bridge.py serve        # 启动桥服务 (默认)
    python bridge.py sync         # 扫描并导入免费模型到 WorkBuddy
    python bridge.py sync --fast  # 跳过探测, 直接全部导入
    python bridge.py uninstall    # 从 WorkBuddy 移除本桥导入的模型
    python bridge.py status       # 检查环境状态
"""
import argparse
import atexit
import base64
import hashlib
import json
import lzma
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 保证 Windows 控制台中文输出正常 (统一 UTF-8, 老 cmd 请先 chcp 65001)
for _s in (sys.stdout, sys.stderr):
    try:
        if _s and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent


def app_dir():
    """程序目录: 打包后 = exe 所在目录; 若 exe 在 tools/ 子目录下则取上一层 (主目录);
    脚本运行 = 脚本目录。"""
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).resolve().parent
        if p.name.lower() == "tools" and (p.parent / "OCBridge.exe").exists():
            return p.parent
        return p
    return HERE


APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / "config.json"
CACHE_PATH = APP_DIR / "models_cache.json"
PROBE_CACHE_PATH = APP_DIR / "probe_cache.json"
SERVE_LOG = APP_DIR / "opencode_serve.log"

VERSION = "2.2.0"
OWNER = "ocbridge-v2"
OWNER_FIELD = "ocBridgeOwner"

DEFAULT_CONFIG = {
    "port": 8899,
    "host": "127.0.0.1",
    "api_key": "ocbridge-2026",
    "backend": {
        "port": 4999,
        "password": "ocbridge-2026",
        "cli_path": "",
        "idle_timeout": 900,
        "start_timeout": 45,
    },
    "agent": "bridge",
    "request_timeout": 180,
    "session_cache_size": 100,
    "tool_envelope": True,
    "probe": {"timeout": 20, "cache_ttl": 21600},
    "workbuddy_models": "~/.workbuddy/models.json",
}


def materialize_engine():
    """若内置引擎以 .xz 压缩存放, 首次使用时自动解压 (幂等; 检查开销极小)。
    用于"分发包瘦身": 下载包内引擎为压缩态, 首次运行解压一次。"""
    eng_dir = APP_DIR / "opencode"
    for name in ("opencode.exe", "opencode"):
        xz = eng_dir / (name + ".xz")
        exe = eng_dir / name
        try:
            if xz.exists() and not exe.exists():
                print(f"[首次使用] 正在解压内置引擎 ({xz.stat().st_size // 1048576} MB -> 约 200 MB, 请稍候)...", flush=True)
                t0 = time.time()
                with lzma.open(xz, "rb") as fi, open(exe, "wb") as fo:
                    shutil.copyfileobj(fi, fo, 8 * 1024 * 1024)
                if os.name != "nt":
                    os.chmod(exe, 0o755)
                print(f"[完成] 引擎已就绪 (耗时 {time.time() - t0:.0f} 秒)", flush=True)
                return str(exe)
        except Exception as e:
            print(f"[警告] 解压内置引擎失败: {e}", flush=True)
    return None


def find_opencode_cli(cfg=None, allow_extract=True):
    """自动探测本机 opencode 可执行文件。返回路径字符串或 None。
    allow_extract=False 时不会触发内置引擎的首次解压 (供 UI 状态刷新使用)。"""
    if allow_extract:
        materialize_engine()
    candidates = []
    if cfg:
        p = (cfg.get("backend") or {}).get("cli_path")
        if p:
            candidates.append(Path(p))
    # 1) exe/脚本同级目录的 opencode/ (setup --download 下载到这里的)
    candidates.append(APP_DIR / "opencode" / "opencode.exe")
    candidates.append(APP_DIR / "opencode" / "opencode")
    # 2) opencode 桌面版
    la = os.environ.get("LOCALAPPDATA")
    if la:
        candidates.append(Path(la) / "Programs" / "@opencodedesktop" / "resources" / "opencode-cli.exe")
    # 3) 官方 CLI 安装位置 ~/.opencode/bin
    candidates.append(Path.home() / ".opencode" / "bin" / "opencode.exe")
    candidates.append(Path.home() / ".opencode" / "bin" / "opencode")
    # 3b) macOS 常见安装位置 (Homebrew / 手动安装)
    if sys.platform == "darwin":
        candidates.append(Path("/opt/homebrew/bin/opencode"))
        candidates.append(Path("/usr/local/bin/opencode"))
    # 4) PATH 中查找
    for name in ("opencode.exe", "opencode", "opencode-cli.exe", "opencode-cli"):
        w = shutil.which(name)
        if w:
            candidates.append(Path(w))
    for c in candidates:
        try:
            if c and c.exists() and c.is_file():
                return str(c)
        except Exception:
            pass
    return None


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] config.json 解析失败({e}), 使用默认配置")
            return cfg
        # 兼容 v1 的 opencode.* 字段
        if "opencode" in user and "backend" not in user:
            oc = user.pop("opencode")
            user["backend"] = {
                "port": int(str(oc.get("server_url", ":4999")).rsplit(":", 1)[-1]),
                "password": oc.get("password", cfg["backend"]["password"]),
                "cli_path": oc.get("cli_path", cfg["backend"]["cli_path"]),
            }
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def models_file_path(cfg):
    p = cfg.get("workbuddy_models") or "~/.workbuddy/models.json"
    return Path(os.path.expanduser(p))


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # 双通道: 同时直写日志文件 (不依赖启动方式的 stdout 重定向)
    try:
        with open(SERVE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ====================================================================
# 登录态检查
# ====================================================================
def check_login():
    """检查本机 opencode 是否已登录 (免费模型要求登录)。返回 (ok, message)。"""
    db = opencode_data_dir() / "opencode.db"
    if not db.exists():
        return False, "未找到 opencode 数据库, 请先安装并登录 OpenCode 客户端"
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT count(*) FROM credential WHERE integration_id='opencode' AND active=1"
            ).fetchone()
        finally:
            con.close()
        if row and row[0] > 0:
            return True, "已登录 (OpenCode Console)"
        return False, "opencode 未登录。免费模型需要登录: 打开 OpenCode 客户端完成登录后重试"
    except Exception as e:
        return False, f"无法读取 opencode 凭据: {e}"


# ====================================================================
# opencode 后端管理 (懒启动 + 空闲自退)
# ====================================================================
class BackendError(RuntimeError):
    pass


BRIDGE_AGENT_NAME = "bridge"
BRIDGE_OC_CONFIG = {
    "websearch": False,
    "permission": {"websearch": "deny", "bash": "deny"},
    "agent": {
        BRIDGE_AGENT_NAME: {
            "description": "OCBridge text-only bridge agent",
            "mode": "primary",
            "system": (
                "You are a pure text assistant inside a bridge service. "
                "You have NO web search and NO command execution. "
                "Answer directly in plain text. If the user message specifies "
                "an output format (e.g. a JSON envelope), follow it exactly."
            ),
        }
    },
}


def ensure_bridge_oc_config():
    """确保注入给 opencode 后端的配置文件存在且为最新; 返回路径。
    关键作用: 关闭 websearch(该工具在本场景下会永久挂起) 并禁用 bash。"""
    path = APP_DIR / "opencode_bridge.json"
    content = json.dumps(BRIDGE_OC_CONFIG, ensure_ascii=False, indent=2)
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
    except Exception as e:
        log(f"[警告] 写入 opencode 配置失败: {e}")
    return str(path)


class BackendManager:
    def __init__(self, cfg):
        b = cfg["backend"]
        self.cfg = cfg
        self.port = int(b["port"])
        self.password = b["password"]
        self.cli_path = find_opencode_cli(cfg)
        self.idle_timeout = int(b.get("idle_timeout", 300))
        self.start_timeout = int(b.get("start_timeout", 45))
        self.base = f"http://127.0.0.1:{self.port}"
        self._auth = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        self._lock = threading.Lock()
        self._proc = None          # 我们 spawn 的进程; None = 复用的外部实例
        self._ready = False
        self._starting = False
        self._inflight = 0
        self._last_active = time.time()
        self._janitor_stop = threading.Event()
        self._janitor = threading.Thread(target=self._janitor_loop, daemon=True)
        self._janitor.start()

    # ---------- 基础 HTTP ----------
    def _req(self, method, path, body=None, timeout=30):
        import urllib.request
        import urllib.error
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Basic {self._auth}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw else {})

    def health(self, timeout=3):
        try:
            code, _ = self._req("GET", "/api/info", timeout=timeout)
            return code == 200
        except Exception:
            return False

    # ---------- 生命周期 ----------
    def touch(self):
        self._last_active = time.time()

    def inc_inflight(self):
        with self._lock:
            self._inflight += 1

    def dec_inflight(self):
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._last_active = time.time()

    def ensure(self):
        """确保后端可用; 必要时拉起。并发安全。"""
        if self._ready and self.health():
            return
        with self._lock:
            if self._ready and self.health():
                return
            if self._starting:
                # 等在途的启动完成
                deadline = time.time() + self.start_timeout + 5
                while self._starting and time.time() < deadline:
                    time.sleep(0.5)
                if self._ready:
                    return
            # 1) 已有实例可用?
            if self.health():
                self._ready = True
                self._proc = None      # 外部实例, 不归我们管
                log(f"复用已有 opencode server (127.0.0.1:{self.port})")
                return
            # 2) 端口被占但连不上 -> 不硬上
            if self._port_in_use():
                raise BackendError(
                    f"端口 {self.port} 已被其他程序占用且无法用配置密码连接。"
                    f"请修改 config.json 的 backend.port"
                )
            # 3) 拉起 (失败自动重试一次: 兼容端口未完全释放等瞬时状况)
            self._starting = True
            try:
                last_err = None
                for attempt in (1, 2):
                    if attempt == 2:
                        time.sleep(2)
                        if self.health():          # 竞态: 其它实例已拉起
                            self._ready = True
                            self._proc = None
                            log(f"复用已有 opencode server (127.0.0.1:{self.port})")
                            return
                    try:
                        self._spawn()
                        deadline = time.time() + self.start_timeout
                        while time.time() < deadline:
                            if self._proc and self._proc.poll() is not None:
                                raise BackendError(f"opencode server 启动失败(退出码 {self._proc.returncode}), 见 {SERVE_LOG.name}")
                            if self.health():
                                self._ready = True
                                log(f"opencode server 已就绪 (127.0.0.1:{self.port}, pid={self._proc.pid if self._proc else '?'})")
                                return
                            time.sleep(1)
                        raise BackendError("opencode server 启动超时")
                    except BackendError as e:
                        last_err = e
                        try:
                            if self._proc and self._proc.poll() is None:
                                self._proc.terminate()
                        except Exception:
                            pass
                        self._proc = None
                        if attempt == 2:
                            raise
                        log(f"opencode server 拉起失败({str(e)[:120]}), 2 秒后重试...")
                if last_err:
                    raise last_err
            finally:
                self._starting = False

    def _port_in_use(self):
        import socket
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", self.port))
            return True
        except Exception:
            return False
        finally:
            s.close()

    def _spawn(self):
        if not self.cli_path or not Path(self.cli_path).exists():
            raise BackendError(
                "找不到 opencode 可执行文件。请安装 OpenCode 客户端, "
                "或运行: ocbridge setup --download  (自动下载 opencode)"
            )
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        env["OPENCODE_SERVER_USERNAME"] = "opencode"
        env["OPENCODE_PASSWORD"] = self.password
        # 防止 opencode 加载工作目录里的指令文件(AGENTS.md/CLAUDE.md 等)污染桥的推理上下文;
        # 上游客户端(WorkBuddy)自己管理上下文, 桥只做纯推理中转。
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
        env["OPENCODE_CONFIG"] = ensure_bridge_oc_config()
        env["OPENCODE_DISABLE_CLAUDE_CODE"] = "true"
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        logf = open(SERVE_LOG, "ab")
        logf.write(f"\n==== [{time.strftime('%Y-%m-%d %H:%M:%S')}] spawn serve :{self.port} ====\n".encode())
        logf.flush()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(
            [self.cli_path, "serve", "--port", str(self.port), "--hostname", "127.0.0.1"],
            env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=str(HERE), **kwargs,
        )

    def stop(self, reason=""):
        """停掉我们 spawn 的实例 (不动外部实例)。"""
        with self._lock:
            self._ready = False
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            if reason:
                log(f"停止 opencode server (pid={proc.pid}): {reason}")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        if proc is not None:
            # 等端口真正释放, 避免下一次拉起撞上尚未关闭的监听
            for _ in range(30):
                if not self._port_in_use():
                    break
                time.sleep(0.2)

    def shutdown(self):
        self._janitor_stop.set()
        self.stop(reason="桥退出")

    def _janitor_loop(self):
        while not self._janitor_stop.wait(10):
            try:
                with self._lock:
                    idle_stop = (
                        self._ready and self._inflight == 0 and self._proc is not None
                        and time.time() - self._last_active >= self.idle_timeout
                    )
                if idle_stop:
                    self.stop(reason=f"空闲超时({self.idle_timeout}s), 自动退出省内存")
            except Exception:
                pass

    # ---------- 业务 ----------
    def api(self, method, path, body=None, timeout=30):
        try:
            return self._req(method, path, body, timeout=timeout)
        except Exception as e:
            raise BackendError(f"opencode API 调用失败: {e}")

    def fetch_models(self):
        code, data = self.api("GET", "/api/model")
        models = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(models, list):
            raise BackendError("模型列表格式异常")
        return [m for m in models if isinstance(m, dict) and m.get("id")]

    def create_session(self, model_id, title="bridge"):
        body = {"model": {"id": model_id, "providerID": "opencode"}, "title": title}
        agent = self.cfg.get("agent")
        if agent:
            body["agent"] = agent
        code, data = self.api("POST", "/api/session", body)
        return data["data"]["id"]

    def send_prompt(self, sid, text):
        self.api("POST", f"/api/session/{sid}/prompt", {"text": text})

    def messages(self, sid):
        code, data = self.api("GET", f"/api/session/{sid}/message")
        return data.get("data", [])

    def wait_reply(self, sid, since_ms, max_wait=None, poll=0.5):
        deadline = time.time() + (max_wait or self.cfg.get("request_timeout", 300))
        stall_since = None      # 检测"工具卡死": running 状态的工具持续过久
        while time.time() < deadline:
            msgs = self.messages(sid)
            asst, idle = None, None
            for m in msgs:
                t = m.get("time", {}) or {}
                if m.get("type") == "assistant" and t.get("created", 0) >= since_ms:
                    asst = m
                if m.get("type") == "idle" and t.get("created", 0) >= since_ms:
                    idle = m
            if asst is not None:
                text = "".join(p.get("text", "") for p in asst.get("content", []) if p.get("type") == "text")
                done = asst.get("time", {}).get("completed") is not None
                if text and (done or idle):
                    return text, {"tokens": asst.get("tokens"), "cost": asst.get("cost")}
                # 卡死工具检测: 长时间 running 的工具(如需要客户端参与但无人执行的工具)
                has_running_tool = any(
                    p.get("type") == "tool" and (p.get("state") or {}).get("status") == "running"
                    for p in asst.get("content", [])
                )
                if has_running_tool:
                    if stall_since is None:
                        stall_since = time.time()
                    elif time.time() - stall_since >= 30:
                        raise BackendError("引擎工具执行卡住(30秒无进展), 中断并重试")
                else:
                    stall_since = None
            if idle is not None and idle.get("outcome") == "failed":
                err = idle.get("error") or {}
                raise BackendError(f"模型调用失败: {err.get('message') or err.get('type') or idle.get('outcome')}")
            time.sleep(poll)
        raise BackendError("等待模型回复超时")

    def delete_session(self, sid):
        try:
            self.api("DELETE", f"/api/session/{sid}", timeout=10)
        except Exception:
            pass

    def generate_once(self, model_id, prompt, max_wait=None):
        """一次性问答: 新建 session -> prompt -> 等回复 -> 删除。返回 (text, meta)。"""
        self.ensure()
        sid = self.create_session(model_id, title="bridge-once")
        try:
            t0 = int(time.time() * 1000) - 1500
            self.send_prompt(sid, prompt)
            return self.wait_reply(sid, t0, max_wait=max_wait)
        finally:
            self.delete_session(sid)


# ====================================================================
# 模型发现 / 筛选 (按价格, 不做名字匹配)
# ====================================================================
def model_is_free(m):
    cost = m.get("cost")
    c = (cost[0] if isinstance(cost, list) and cost else cost) or {}
    cache = c.get("cache") or {}
    return (
        c.get("input") == 0 and c.get("output") == 0
        and cache.get("read", 0) == 0 and cache.get("write", 0) == 0
        and m.get("status") != "deprecated"
        and ((m.get("capabilities") or {}).get("output") or {}) != {"text": False}
    )


def to_model_entry(m):
    caps = m.get("capabilities") or {}
    lim = m.get("limit") or {}
    inputs = caps.get("input") or []
    if isinstance(inputs, dict):
        images = bool(inputs.get("image"))
    else:
        images = "image" in inputs
    return {
        "id": m.get("id") or m.get("modelID"),
        "name": m.get("name") or m.get("id"),
        "tools": bool(caps.get("tools")),
        "images": images,
        "context": lim.get("context"),
        "input": lim.get("input") or lim.get("context"),
        "output": lim.get("output"),
    }


def fetch_free_models(backend):
    models = backend.fetch_models()
    free = sorted((to_model_entry(m) for m in models if model_is_free(m)), key=lambda e: e["id"])
    # 缓存给 /v1/models 与离线映射用
    try:
        CACHE_PATH.write_text(json.dumps({
            "updated": int(time.time()), "models": free,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return free


def load_cached_models():
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data.get("models") or []
    except Exception:
        return []


# ====================================================================
# 工具信封 (模型输出 JSON -> OpenAI tool_calls)
# ====================================================================
def _try_json(s):
    try:
        return json.loads(s)
    except Exception:
        return None


def _balanced_object(s, start):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    return None


CALL_KEYS = ("oc_tool_calls", "tool_calls", "calls")


def extract_envelope(text):
    """从回复文本中提取工具信封; 返回 (content, calls) 或 None(非信封)。"""
    if not text:
        return None
    s = text.strip()
    obj = _try_json(s)
    if obj is None:
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.S)
        if m:
            obj = _try_json(m.group(1).strip())
    if obj is None and any(k in s for k in CALL_KEYS):
        idx = 0
        while idx < len(s):
            i = s.find("{", idx)
            if i < 0:
                break
            chunk = _balanced_object(s, i)
            if chunk:
                o = _try_json(chunk)
                if isinstance(o, dict) and any(isinstance(o.get(k), list) for k in CALL_KEYS):
                    obj = o
                    break
            idx = i + 1
    if not isinstance(obj, dict):
        return None
    for k in CALL_KEYS:
        calls = obj.get(k)
        if isinstance(calls, list):
            normalized = normalize_calls(calls)
            content = obj.get("content")
            content = content if isinstance(content, str) else ""
            return content, normalized
    return None


def normalize_calls(calls):
    out = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("tool") or c.get("function", {}).get("name") if isinstance(c.get("function"), dict) else c.get("name") or c.get("tool")
        args = c.get("arguments")
        if args is None and isinstance(c.get("function"), dict):
            args = c["function"].get("arguments")
        if args is None:
            args = c.get("args") or c.get("parameters") or c.get("input") or {}
        if not name:
            continue
        if isinstance(args, str):
            parsed = _try_json(args)
            args = parsed if parsed is not None else {"raw": args}
        if not isinstance(args, (dict, list)):
            args = {"value": args}
        out.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {
                "name": str(name),
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })
    return out


def tools_digest(tools):
    """把 OpenAI tools 定义压缩成提示文本。"""
    lines = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 300:
            desc = desc[:300] + "…"
        params = fn.get("parameters") or {}
        try:
            ps = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            ps = "{}"
        if len(ps) > 1500:
            ps = ps[:1500] + "…"
        lines.append(f"- {name}: {desc}\n  参数schema: {ps}")
    return "\n".join(lines)


ENVELOPE_PROMPT = """【工具调用协议】
你是一个外部 AI 助手的推理组件, 你本身没有本机执行权限(不能读文件/执行命令/访问网络)。
当需要让外部助手执行操作时, 输出且仅输出一个 JSON 对象(不要代码块标记、不要其他文字):
{{"oc_tool_calls":[{{"name":"<工具名>","arguments":{{<JSON参数>}}}}]}}
可用工具:
{tools}
如果不需要执行操作, 直接输出面向用户的文本回复。
重要: 绝不要声称你已执行了任何操作; 所有执行都由外部客户端完成, 结果会以【工具结果】的形式在后续消息中提供。"""


# ====================================================================
# 请求构建 (OpenAI messages -> opencode prompt)
# ====================================================================
def content_to_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") in ("image_url", "input_image"):
                    parts.append("[图片附件: 本桥暂不支持图像输入]")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def build_prompt(messages, tools, use_envelope):
    system_notes, dialog = [], []
    for m in messages:
        role = m.get("role", "user")
        text = content_to_text(m.get("content"))
        if role == "system":
            if text:
                system_notes.append(text)
        elif role == "assistant":
            tcs = m.get("tool_calls") or []
            calls_txt = ""
            if tcs:
                names = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    names.append(f"{fn.get('name')}({fn.get('arguments', '')})")
                calls_txt = "\n[Assistant 发起了工具请求] " + "; ".join(names)
            if text or calls_txt:
                dialog.append(("[Assistant] " + text) if text else "[Assistant]")
                if calls_txt:
                    dialog.append(calls_txt)
        elif role == "tool":
            name = m.get("name") or "tool"
            dialog.append(f"[工具结果: {name}] {text}")
        else:
            if text:
                dialog.append("[User] " + text)

    parts = []
    if system_notes:
        parts.append("【背景指示】\n" + "\n\n".join(system_notes))
    if tools and use_envelope:
        parts.append(ENVELOPE_PROMPT.format(tools=tools_digest(tools)))
    if dialog:
        parts.append("【对话记录】\n" + "\n\n".join(dialog))
    parts.append("【任务】请按要求继续对话, 输出助手的下一步内容。")
    return "\n\n".join(parts)


def build_incremental(messages):
    """增量模式: 只取最后一条 user / tool 消息。"""
    for m in reversed(messages):
        role = m.get("role")
        if role == "tool":
            name = m.get("name") or "tool"
            return f"[工具结果: {name}] {content_to_text(m.get('content'))}"
        if role == "user":
            return content_to_text(m.get("content"))
    return ""


# ====================================================================
# 会话缓存
# ====================================================================
class SessionCache:
    def __init__(self, size, busy):
        self.size = size
        self.lock = threading.Lock()
        self.map = {}
        self.order = []
        self.busy = busy

    @staticmethod
    def key(messages):
        h = hashlib.md5()
        h.update(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return h.hexdigest()

    def get(self, k):
        if not k:
            return None
        with self.lock:
            item = self.map.get(k)
            if not item:
                return None
            sid = item
            if sid in self.busy:
                return None      # 会话正忙, 新开一个
            return sid

    def put(self, k, sid):
        if not k or not sid:
            return
        with self.lock:
            self.map[k] = sid
            if k in self.order:
                self.order.remove(k)
            self.order.append(k)
            while len(self.order) > self.size:
                old = self.order.pop(0)
                self.map.pop(old, None)


def estimate_tokens(s):
    return max(1, len(s) // 4) if s else 0


# ====================================================================
# models.json 同步
# ====================================================================
def is_owned_entry(entry, cfg):
    if not isinstance(entry, dict):
        return False
    if entry.get(OWNER_FIELD) == OWNER:
        return True
    url = str(entry.get("url") or "")
    bridge_host = f"127.0.0.1:{cfg['port']}"
    return bridge_host in url


def merge_models(document, entries):
    """合并规则: 摘掉自己的旧条目, 追加新条目; 用户条目与第三方工具条目一律不动。"""
    if not isinstance(document, list):
        raise RuntimeError("models.json 不是数组格式, 已放弃写入")
    kept = [m for m in document if not is_owned_entry(m, CFG_REF[0])]
    kept_ids = {str(m.get("id")) for m in kept if isinstance(m, dict)}
    added = []
    for e in entries:
        if e["id"] in kept_ids:
            continue
        added.append(e)
    return kept + added, len(added)


CFG_REF = [None]   # merge_models 里需要 cfg; 简单起见用模块引用


def atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp-" + uuid.uuid4().hex[:8])
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def prune_backups(path: Path, keep=5):
    try:
        prefix = path.name + ".ocbridge-"
        baks = sorted(p for p in path.parent.iterdir() if p.name.startswith(prefix) and p.name.endswith(".bak"))
        for old in baks[:-keep]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def sync_models_to_workbuddy(cfg, entries):
    """写入 WorkBuddy models.json (备份 + 原子写 + 只动自己的条目)。"""
    path = models_file_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_text = path.read_text(encoding="utf-8") if path.exists() else "[]"
    document = json.loads(old_text) if old_text.strip() else []
    merged, added = merge_models(document, entries)
    new_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    if document and json.dumps(merged, ensure_ascii=False) == json.dumps(document, ensure_ascii=False):
        return {"changed": False, "added": 0}
    if path.exists():
        bak = path.with_name(path.name + ".ocbridge-" + time.strftime("%Y%m%d-%H%M%S") + ".bak")
        bak.write_text(old_text, encoding="utf-8")
        prune_backups(path)
    atomic_write(path, new_text)
    return {"changed": True, "added": added, "file": str(path)}


# ====================================================================
# 探测 (sync 时检验模型可用性)
# ====================================================================
def load_probe_cache():
    try:
        return json.loads(PROBE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_probe_cache(cache):
    try:
        PROBE_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def probe_models(backend, free_models, cfg):
    cache = load_probe_cache()
    ttl = int(cfg["probe"]["cache_ttl"])
    timeout = int(cfg["probe"]["timeout"])
    results = {}
    now = time.time()
    todo = []
    for m in free_models:
        c = cache.get(m["id"])
        if c and now - c.get("at", 0) < ttl:
            results[m["id"]] = c
        else:
            todo.append(m)
    total = len(free_models)
    done = total - len(todo)
    for i, m in enumerate(todo):
        done += 1
        mid = m["id"]
        print(f"  探测 [{done}/{total}] {mid} ... ", end="", flush=True)
        try:
            t0 = time.time()
            sid = backend.create_session(mid, title="probe")
            try:
                ms = int(time.time() * 1000) - 1500
                backend.send_prompt(sid, "Reply with exactly: OK")
                text, _meta = backend.wait_reply(sid, ms, max_wait=timeout)
                dt = time.time() - t0
                print(f"OK ({dt:.1f}s) {text[:30]!r}")
                results[mid] = {"ok": True, "at": now, "latency": round(dt, 1)}
            finally:
                backend.delete_session(sid)
        except Exception as e:
            msg = str(e)
            for _, v in results.items():
                pass
            print(f"FAIL: {msg[:120]}")
            results[mid] = {"ok": False, "at": now, "error": msg[:200]}
    save_probe_cache(results)
    return results


def make_entry(m, cfg):
    bridge_url = f"http://127.0.0.1:{cfg['port']}/v1/chat/completions"
    supports_tools = bool(m.get("tools")) and bool(cfg.get("tool_envelope", True))
    entry = {
        "id": "oc-" + m["id"],
        "name": "OC · " + (m.get("name") or m["id"]),
        "vendor": "Custom",
        "url": bridge_url,
        "apiKey": cfg.get("api_key") or "ocbridge",
        "supportsToolCall": supports_tools,
        "supportsImages": False,
        "useCustomProtocol": False,
        OWNER_FIELD: OWNER,
    }
    if m.get("input"):
        entry["maxInputTokens"] = int(m["input"])
    if m.get("output"):
        entry["maxOutputTokens"] = int(m["output"])
    return entry


# ====================================================================
# HTTP 服务 (OpenAI 兼容)
# ====================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = f"OCBridge/{VERSION}"
    protocol_version = "HTTP/1.1"

    cfg = None
    backend = None
    cache = None
    busy = None

    def log_message(self, fmt, *args):
        log(fmt % args)

    # ---------- helpers ----------
    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code, message, err_type="invalid_request_error"):
        self._json(code, {"error": {"message": message, "type": err_type, "code": code}})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/healthz"):
            login_ok, login_msg = check_login()
            self._json(200 if login_ok else 503, {
                "ok": login_ok,
                "version": VERSION,
                "login": login_msg,
                "backend": self.backend.base,
                "backend_ready": self.backend._ready,
                "models_cached": len(load_cached_models()),
            })
        elif path == "/v1/models":
            models = load_cached_models()
            now = int(time.time())
            data = [{
                "id": "oc-" + m["id"], "object": "model", "created": now, "owned_by": "opencode",
            } for m in models] or [{
                "id": "oc-deepseek-v4.1-flash-free", "object": "model", "created": now, "owned_by": "opencode",
            }]
            self._json(200, {"object": "list", "data": data})
        else:
            self._err(404, f"未知路径 {path}")

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._err(404, f"未知路径 {path}")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._err(400, f"请求体解析失败: {e}")
            return
        try:
            self.handle_chat(body)
        except BackendError as e:
            log(f"chat 失败(502): {e}")
            self._err(502, str(e), err_type="upstream_error")
        except Exception as e:
            log(f"chat 内部错误(500): {type(e).__name__}: {e}")
            self._err(500, f"桥内部错误: {e}", err_type="server_error")

    # ---------- 核心 ----------
    def resolve_model(self, requested):
        """'oc-xxx' -> 'xxx'; 也兼容裸 'xxx' (v1 配置) 与任意名。"""
        models = {m["id"] for m in load_cached_models()}
        if requested in models:
            return requested
        if requested.startswith("oc-"):
            return requested[3:]
        if not requested:
            return "deepseek-v4.1-flash-free"
        return requested

    def handle_chat(self, body):
        cfg, backend, cache, busy = self.cfg, self.backend, self.cache, self.busy
        messages = body.get("messages") or []
        stream = bool(body.get("stream"))
        tools = body.get("tools") or []
        public_model = str(body.get("model") or "oc-deepseek-v4.1-flash-free")
        oc_model = self.resolve_model(public_model)
        if not messages:
            self._err(400, "messages 为空")
            return

        # 登录检查
        login_ok, login_msg = check_login()
        if not login_ok:
            self._err(503, f"OpenCode 未登录, 免费模型不可用。{login_msg}", err_type="login_required")
            return

        backend.inc_inflight()          # 先标记在途, 防止与空闲自退竞态
        backend.touch()
        sid = None
        try:
            backend.ensure()

            t0 = time.time()
            for _attempt in (1, 2, 3):
                prefix_key = SessionCache.key(messages[:-1]) if (len(messages) > 1 and _attempt == 1) else None
                sid = cache.get(prefix_key) if prefix_key else None
                incremental = sid is not None
                if sid is not None:
                    busy.add(sid)
                try:
                    if sid is None:
                        sid = backend.create_session(oc_model, title="bridge")
                        busy.add(sid)
                        prompt = build_prompt(messages, tools, cfg.get("tool_envelope", True))
                    else:
                        prompt = build_incremental(messages)
                    since_ms = int(time.time() * 1000) - 1500
                    try:
                        backend.send_prompt(sid, prompt)
                        text, meta = backend.wait_reply(sid, since_ms)
                    except Exception:
                        if incremental:
                            # 增量失败 -> 全量重试一次
                            cache.put(prefix_key, None)
                            busy.discard(sid)
                            sid = backend.create_session(oc_model, title="bridge")
                            busy.add(sid)
                            prompt = build_prompt(messages, tools, cfg.get("tool_envelope", True))
                            since_ms = int(time.time() * 1000) - 1500
                            backend.send_prompt(sid, prompt)
                            text, meta = backend.wait_reply(sid, since_ms)
                        else:
                            raise
                    cache.put(SessionCache.key(messages), sid)
                    break
                except Exception as e:
                    try:
                        busy.discard(sid)
                    except Exception:
                        pass
                    if _attempt == 3:
                        raise
                    # 后端可能处于"退出中/半死"状态: 清理重建, 重试一次
                    log(f"chat 首次尝试失败({type(e).__name__}: {str(e)[:140]}), 重建后端并重试...")
                    try:
                        backend.stop(reason="失败重试前清理")
                    except Exception:
                        pass
                    backend.touch()
                    backend.ensure()

            # 信封解析 (仅当请求携带工具)
            content, tool_calls = text, []
            if tools and cfg.get("tool_envelope", True):
                parsed = extract_envelope(text)
                if parsed is not None and parsed[1]:
                    content, tool_calls = parsed

            latency = time.time() - t0
            tok = (meta or {}).get("tokens") or {}
            extra = f" tools={len(tool_calls)}" if tool_calls else ""
            self.log_message("chat ok model=%s len=%d in=%s out=%s %.1fs incr=%s%s",
                             public_model, len(content or ""), tok.get("input"), tok.get("output"),
                             latency, incremental, extra)

            cid = "chatcmpl-" + uuid.uuid4().hex[:24]
            created = int(time.time())
            usage = {
                "prompt_tokens": tok.get("input") or 0,
                "completion_tokens": tok.get("output") or 0,
                "total_tokens": (tok.get("input") or 0) + (tok.get("output") or 0),
            }
            if stream:
                self._stream(cid, created, public_model, content or "", tool_calls)
            else:
                if tool_calls:
                    msg = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
                    finish = "tool_calls"
                else:
                    msg = {"role": "assistant", "content": content or ""}
                    finish = "stop"
                self._json(200, {
                    "id": cid, "object": "chat.completion", "created": created,
                    "model": public_model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                    "usage": usage,
                })
        finally:
            try:
                if sid is not None:
                    busy.discard(sid)
            except Exception:
                pass
            backend.dec_inflight()

    def _stream(self, cid, created, model, text, tool_calls):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def chunk(delta=None, finish=None):
            obj = {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))

        try:
            chunk(delta={"role": "assistant"})
            if tool_calls:
                chunk(delta={"tool_calls": tool_calls})
                chunk(finish="tool_calls")
            else:
                step = 24
                for i in range(0, len(text), step):
                    chunk(delta={"content": text[i:i + step]})
                    try:
                        self.wfile.flush()
                    except Exception:
                        return
                    time.sleep(0.01)
                chunk(finish="stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


# ====================================================================
# 命令实现
# ====================================================================
def cmd_status(cfg):
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 状态检查")
    print("=" * 62)
    ok, msg = check_login()
    print(f" 登录状态   : {'OK' if ok else '未登录!'} — {msg}")
    cli = find_opencode_cli(cfg, allow_extract=False)
    if cli:
        print(f" opencode   : {cli}")
    elif (APP_DIR / "opencode" / "opencode.exe.xz").exists():
        print(" opencode   : 内置引擎(压缩存放) — 首次启动服务/同步时自动解压一次")
    else:
        print(" opencode   : 未找到 (运行 setup --download 自动下载)")
    backend = BackendManager(cfg)
    try:
        ready = backend.health()
        print(f" 后端服务   : {'运行中' if ready else '未运行(请求时自动拉起)'} @ 127.0.0.1:{backend.port}")
        if ready or ok:
            backend.ensure()
            free = fetch_free_models(backend)
            print(f" 免费模型   : {len(free)} 个")
            for m in free:
                print(f"    - oc-{m['id']:42s} tools={'Y' if m['tools'] else 'n'} ctx={m['context']}")
    except Exception as e:
        print(f" 后端检查失败: {e}")
    finally:
        backend.shutdown()
    p = models_file_path(cfg)
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        owned = [m for m in doc if is_owned_entry(m, cfg)]
        print(f" WorkBuddy  : {p}")
        print(f" 已导入条目 : {len(owned)} 个 (总 {len(doc)} 条)")
    except Exception as e:
        print(f" WorkBuddy 配置读取失败: {e}")


def cmd_sync(cfg, fast=False):
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 同步免费模型到 WorkBuddy")
    print("=" * 62)
    ok, msg = check_login()
    if not ok:
        print(f" [错误] {msg}")
        return 1
    print(f" 登录状态: OK")
    backend = BackendManager(cfg)
    try:
        backend.ensure()
        free = fetch_free_models(backend)
        print(f" 发现 {len(free)} 个免费模型 (按价格筛选):")
        for m in free:
            print(f"   - {m['id']}")
        if fast:
            results = {m["id"]: {"ok": True} for m in free}
            print(" [--fast] 跳过可用性探测")
        else:
            print(" 开始可用性探测 (有 6 小时缓存):")
            results = probe_models(backend, free, cfg)
        usable = [m for m in free if results.get(m["id"], {}).get("ok")]
        skipped = [m for m in free if not results.get(m["id"], {}).get("ok")]
        print(f" 可用 {len(usable)} 个" + (f", 不可用 {len(skipped)} 个: {[m['id'] for m in skipped]}" if skipped else ""))
        entries = [make_entry(m, cfg) for m in usable]
        result = sync_models_to_workbuddy(cfg, entries)
        if result["changed"]:
            print(f" [完成] 已写入 {result['added']} 条到 {result['file']} (已备份)")
        else:
            print(" [完成] 配置无变化, 未写入")
        print(" 提示: 重启 WorkBuddy 客户端后可在模型选择器中看到 'OC · xxx' 模型")
        return 0
    except Exception as e:
        print(f" [错误] {e}")
        return 1
    finally:
        backend.shutdown()


def cmd_uninstall(cfg):
    path = models_file_path(cfg)
    if not path.exists():
        print(" models.json 不存在, 无需清理")
        return 0
    old_text = path.read_text(encoding="utf-8")
    doc = json.loads(old_text)
    merged, _ = merge_models(doc, [])
    removed = len([m for m in doc if is_owned_entry(m, cfg)]) - len([m for m in merged if is_owned_entry(m, cfg)])
    if json.dumps(merged, ensure_ascii=False) == json.dumps(doc, ensure_ascii=False):
        print(" 没有本桥导入的条目, 无需清理")
        return 0
    bak = path.with_name(path.name + ".ocbridge-" + time.strftime("%Y%m%d-%H%M%S") + ".bak")
    bak.write_text(old_text, encoding="utf-8")
    atomic_write(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    print(f" [完成] 已移除本桥导入的模型条目 (备份: {bak.name})")
    return 0


# ====================================================================
# 登录态迁移 (export-auth / import-auth / setup / 下载)
# ====================================================================
def opencode_data_dir():
    """opencode 数据目录 (含 opencode.db)。
    Windows/Linux 统一 XDG 风格 ~/.local/share/opencode; macOS 追加 Library 回退。"""
    cands = [Path.home() / ".local" / "share" / "opencode"]
    if sys.platform == "darwin":
        cands.append(Path.home() / "Library" / "Application Support" / "opencode")
    for c in cands:
        if (c / "opencode.db").exists():
            return c
    return cands[0]


def _read_credentials(db_path):
    """从 opencode.db 读取 opencode 集成凭据 (官方 auth export 同构)。"""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(credential)").fetchall()]
        if not cols:
            return []
        rows = con.execute(
            "SELECT * FROM credential WHERE integration_id='opencode' AND active=1").fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            value = d.get("value")
            try:
                value = json.loads(value)
            except Exception:
                pass
            out.append({
                "id": d.get("id"),
                "integrationID": d.get("integration_id"),
                "label": d.get("label"),
                "active": bool(d.get("active")),
                "value": value,
            })
        return out
    finally:
        con.close()


def cmd_export_auth(cfg, args):
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 提取 opencode 登录状态")
    print("=" * 62)
    db = Path(args.db) if args.db else (opencode_data_dir() / "opencode.db")
    if not db.exists():
        print(f" [错误] 未找到 opencode 数据库: {db}")
        print("        请先安装并登录 OpenCode 客户端")
        return 1
    creds = _read_credentials(db)
    if not creds:
        print(" [错误] 数据库中没有有效的 opencode 登录凭据")
        print("        请在 OpenCode 客户端完成登录后重试")
        return 1
    out_path = Path(args.output) if args.output else (APP_DIR / "ocbridge-auth.json")
    out_path.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f" [OK] 已导出 {len(creds)} 条登录凭据 -> {out_path}")
    for c in creds:
        v = c.get("value") or {}
        meta = v.get("metadata") or {}
        exp = v.get("expires")
        exp_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp / 1000)) if exp else "?"
        print(f"     账号: {meta.get('email', '?')}  组织: {meta.get('orgName', '?')}  令牌到期: {exp_s}")
    print()
    print(" 换电脑用法: 把这个文件拷贝到新电脑, 运行 `ocbridge import-auth <文件>`")
    print(" 注意: 文件包含登录凭据, 请勿外传。")
    return 0


def cmd_import_auth(cfg, args):
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 导入 opencode 登录状态")
    print("=" * 62)
    src = Path(args.file)
    if not src.exists():
        print(f" [错误] 凭据文件不存在: {src}")
        return 1
    try:
        creds = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f" [错误] 凭据文件解析失败: {e}")
        return 1
    if not isinstance(creds, list) or not creds:
        print(" [错误] 凭据文件格式不正确 (应为 ocbridge export-auth 导出的 JSON)")
        return 1
    isolate = bool(args.data_dir)
    data_dir = Path(args.data_dir) if args.data_dir else opencode_data_dir()
    db = data_dir / "opencode.db"
    if not db.exists():
        cli = find_opencode_cli(cfg)
        if not cli:
            print(f" [错误] 目标数据库不存在 ({db}), 且未找到 opencode 可执行文件")
            print("        请先安装 OpenCode 或运行 `ocbridge setup --download`")
            return 1
        print(f" 目标数据库不存在, 正在用 opencode 初始化 (最多 30 秒)...")
        if not _init_opencode_db(cli, data_dir, isolate=isolate):
            print(" [错误] 初始化失败, 请手动运行一次 opencode 后重试")
            return 1
    con = sqlite3.connect(db, timeout=15)
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(credential)").fetchall()]
        if not cols:
            print(" [错误] 目标数据库缺少 credential 表, 版本不兼容")
            return 1
        con.execute("DELETE FROM credential WHERE integration_id='opencode'")
        now = int(time.time() * 1000)
        n = 0
        for c in creds:
            if not isinstance(c, dict):
                continue
            value = c.get("value")
            value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            con.execute(
                "INSERT INTO credential (id, integration_id, label, value, active, time_created, time_updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (c.get("id") or ("cred_" + uuid.uuid4().hex[:20]),
                 c.get("integrationID") or "opencode",
                 c.get("label") or "Personal",
                 value, 1, now, now))
            n += 1
        con.commit()
        exist = con.execute(
            "SELECT COUNT(*) FROM credential WHERE integration_id='opencode' AND active=1").fetchone()[0]
    finally:
        con.close()
    print(f" [OK] 已写入 {n} 条凭据 -> {db} (当前有效 {exist} 条)")
    print(" 下一步: 运行 `ocbridge status` 检查, 或 `ocbridge serve` 启动桥")
    return 0


def _free_port():
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _init_opencode_db(cli, data_dir, isolate=False):
    """用 opencode 初始化数据目录 (起一个临时 serve, 等 db 生成后停掉)。"""
    env = dict(os.environ)
    if isolate:
        parent = str(Path(data_dir).parent)
        env["XDG_DATA_HOME"] = parent
        env["XDG_CONFIG_HOME"] = os.path.join(parent, "config")
        env["XDG_STATE_HOME"] = os.path.join(parent, "state")
        env["XDG_CACHE_HOME"] = os.path.join(parent, "cache")
    env["OPENCODE_SERVER_PASSWORD"] = "init-" + uuid.uuid4().hex[:10]
    env["OPENCODE_SERVER_USERNAME"] = "opencode"
    logf = open(APP_DIR / "init.log", "ab")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    try:
        proc = subprocess.Popen(
            [cli, "serve", "--port", str(_free_port())],
            env=env, stdout=logf, stderr=subprocess.STDOUT, **kwargs)
    except Exception as e:
        print(f"   启动失败: {e}")
        return False
    db = Path(data_dir) / "opencode.db"
    deadline = time.time() + 30
    try:
        while time.time() < deadline:
            if db.exists():
                time.sleep(2)     # 等写入稳定
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.8)
        return db.exists()
    finally:
        try:
            proc.terminate(); proc.wait(timeout=6)
        except Exception:
            try: proc.kill()
            except Exception: pass


def download_opencode():
    """按官方安装器逻辑从 npm 下载 opencode 二进制到 <程序目录>/opencode/。"""
    os_key = "windows" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
    mach = platform.machine().lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "x64"
    target = f"{os_key}-{arch}"
    binary_name = "opencode.exe" if os.name == "nt" else "opencode"

    def _get_json(url, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": "ocbridge"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    version, scope = None, "@opencode"
    try:
        md = _get_json("https://opencode.ai/update/api/latest/cli/npm")
        version = md.get("version")
        pkg = (md.get("metadata") or {}).get("package") or md.get("package") or ""
        if "/" in pkg:
            scope = pkg.rsplit("/", 1)[0]
    except Exception as e:
        print(f"   版本查询失败: {e}")
    if not version:
        print("   无法获取 opencode 版本信息 (网络不可达)")
        return None
    pkg_name = f"{scope}/cli-{target}"
    file_base = f"cli-{target}-{version}.tgz"
    registries = [
        f"https://registry.npmmirror.com/{pkg_name}/-/{file_base}",
        f"https://registry.npmjs.org/{pkg_name}/-/{file_base}",
    ]
    dest_dir = APP_DIR / "opencode"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / binary_name
    tgz = dest_dir / file_base
    ok = False
    for url in registries:
        try:
            host = url.split("/")[2]
            print(f"   下载 opencode {version} from {host} (约 100MB, 请稍候)...")
            req = urllib.request.Request(url, headers={"User-Agent": "ocbridge"})
            with urllib.request.urlopen(req, timeout=600) as r, open(tgz, "wb") as f:
                shutil.copyfileobj(r, f)
            ok = True
            break
        except Exception as e:
            print(f"   下载失败: {e}")
    if not ok:
        return None
    try:
        with tarfile.open(tgz, "r:gz") as tf:
            member = None
            for m in tf.getmembers():
                if m.name.endswith("bin/" + binary_name) and m.isfile():
                    member = m
                    break
            if not member:
                print("   tgz 包中未找到二进制文件")
                return None
            srcf = tf.extractfile(member)
            with open(dest, "wb") as out:
                shutil.copyfileobj(srcf, out)
    except Exception as e:
        print(f"   解压失败: {e}")
        return None
    finally:
        try:
            tgz.unlink()
        except Exception:
            pass
    if os.name != "nt":
        os.chmod(dest, 0o755)
    print(f"   完成: {dest} ({dest.stat().st_size // 1048576} MB, v{version})")
    return str(dest)


def cmd_setup(cfg, args):
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 环境准备")
    print("=" * 62)
    cli = find_opencode_cli(cfg)
    if cli:
        print(f" [OK] opencode 二进制: {cli}")
    else:
        print(" [--] 未找到 opencode 二进制")
        if args.download:
            cli = download_opencode()
            if cli:
                print(f" [OK] 已下载: {cli}")
            else:
                print(" [错误] 下载失败, 请检查网络后重试")
        else:
            print("      运行 `ocbridge setup --download` 自动下载, 或安装 OpenCode 客户端")
    ok, msg = check_login()
    if ok:
        print(f" [OK] 登录状态: {msg}")
    else:
        print(f" [!] 登录状态: {msg}")
        print("     换电脑迁移: 在已登录的电脑运行 `ocbridge export-auth`,")
        print("     把 ocbridge-auth.json 拷到本机后运行 `ocbridge import-auth <文件>`")
    p = models_file_path(cfg)
    try:
        if p.exists():
            doc = json.loads(p.read_text(encoding="utf-8"))
            mine = [m for m in doc if is_owned_entry(m, cfg)]
            print(f" [OK] WorkBuddy 配置: {p} (本桥条目 {len(mine)}/{len(doc)})")
            if not mine:
                print("     运行 `ocbridge sync` 导入免费模型")
        else:
            print(f" [--] 未找到 WorkBuddy 配置: {p} (装好 WorkBuddy 后运行 ocbridge sync)")
    except Exception as e:
        print(f" [!] WorkBuddy 配置读取失败: {e}")
    print(f"\n 本程序目录: {APP_DIR}")
    print(" 下一步: 双击本程序或运行 `ocbridge serve` 启动桥服务")
    return 0


# ====================================================================
# 内建登录 (OAuth Device Flow, 不依赖 opencode 客户端)
# ====================================================================
LOGIN_UA = "opencode/latest/2.0.29/cli"   # 服务端校验客户端 UA
CONSOLE_BASE = "https://opencode.ai"


def _post_json(url, body, ua=LOGIN_UA, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": ua,
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class DeviceLoginError(Exception):
    pass


def start_device_login():
    """第一步: 请求设备码。返回 dict(device_code, user_code, verification_url, interval, expires_in)。"""
    data = _post_json(f"{CONSOLE_BASE}/console/auth/device/code",
                      {"client_id": "opencode-cli", "supports_org_scope": True})
    uri = data.get("verification_uri_complete") or data.get("verification_uri") or "/console/device"
    if uri.startswith("/"):
        uri = CONSOLE_BASE + uri
    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_url": uri,
        "interval": int(data.get("interval") or 5),
        "expires_in": int(data.get("expires_in") or 600),
    }


def poll_device_login(device_code, interval=5, max_wait=600, progress=None):
    """第二步: 轮询直到用户确认。返回 token dict 或抛 DeviceLoginError。"""
    deadline = time.time() + max_wait
    iv = max(2, int(interval))
    while time.time() < deadline:
        time.sleep(iv)
        if progress:
            try:
                progress(int(max(0, deadline - time.time())))
            except Exception:
                pass
        try:
            data = _post_json(f"{CONSOLE_BASE}/console/auth/device/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": "opencode-cli",
            }, timeout=20)
            if data.get("access_token"):
                return data
            raise DeviceLoginError(f"响应异常: {str(data)[:150]}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            low = text.lower()
            if "authorization_pending" in low:
                continue
            if "slow_down" in low:
                iv += 3
                continue
            if "expired" in low:
                raise DeviceLoginError("设备码已过期, 请重新发起登录")
            if "denied" in low:
                raise DeviceLoginError("授权被拒绝")
            raise DeviceLoginError(f"登录失败 HTTP {e.code}: {text[:200]}")
        except DeviceLoginError:
            raise
        except Exception:
            continue   # 网络抖动, 继续轮询
    raise DeviceLoginError("等待授权超时, 请重新发起登录")


def save_login_credentials(token_data, data_dir=None, init_cli=None):
    """把登录得到的 token 写入 opencode 数据库。返回 db 路径。"""
    isolate = bool(data_dir)
    data_dir = Path(data_dir) if data_dir else opencode_data_dir()
    db = data_dir / "opencode.db"
    if not db.exists():
        cli = init_cli or find_opencode_cli(CFG_REF[0])
        if not cli:
            raise RuntimeError("数据库不存在且未找到 opencode, 无法保存登录")
        if not _init_opencode_db(cli, data_dir, isolate=isolate):
            raise RuntimeError("opencode 初始化失败")
    now = int(time.time() * 1000)
    value = {
        "type": "oauth",
        "methodID": "device",
        "refresh": token_data.get("refresh_token"),
        "access": token_data.get("access_token"),
        "expires": now + int(token_data.get("expires_in") or 2592000) * 1000,
        "metadata": {
            "server": CONSOLE_BASE + "/console",
            "orgID": token_data.get("org_id"),
            "orgName": "Personal",
        },
    }
    con = sqlite3.connect(db, timeout=15)
    try:
        con.execute("DELETE FROM credential WHERE integration_id='opencode'")
        con.execute(
            "INSERT INTO credential (id, integration_id, label, value, active, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?)",
            ("cred_login_" + uuid.uuid4().hex[:16], "opencode", "Personal",
             json.dumps(value, ensure_ascii=False), 1, now, now))
        con.commit()
    finally:
        con.close()
    return str(db)


def cmd_login(cfg, args):
    """交互式登录 (device flow, 无需安装 opencode 客户端)。"""
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 登录 OpenCode")
    print("=" * 62)
    try:
        dev = start_device_login()
    except Exception as e:
        print(f" [错误] 无法发起登录: {e}")
        return 1
    print()
    print(f"  步骤1: 在浏览器打开:  {dev['verification_url']}")
    print(f"  步骤2: 登录并输入代码: {dev['user_code']}")
    print()
    try:
        import webbrowser
        webbrowser.open(dev["verification_url"])
        print("  (已尝试自动打开浏览器)")
    except Exception:
        pass
    print(f"  等待确认中 (最多 {dev['expires_in']} 秒)...")
    try:
        tok = poll_device_login(dev["device_code"], dev["interval"], dev["expires_in"])
    except DeviceLoginError as e:
        print(f" [失败] {e}")
        return 1
    try:
        db = save_login_credentials(tok)
    except Exception as e:
        print(f" [错误] 保存登录凭据失败: {e}")
        return 1
    print(f" [成功] 已登录并保存到 {db} (org: {tok.get('org_id') or '?'})")
    print(" 下一步: 启动桥服务 (`ocbridge serve` 或双击打开界面)")
    return 0


def cmd_refresh_auth(cfg, args):
    """手动续期登录凭据 (30 天)。"""
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION} — 续期登录凭据")
    print("=" * 62)
    db = opencode_data_dir() / "opencode.db"
    if not db.exists():
        print(f" [错误] 未找到 {db}")
        return 1
    creds = _read_credentials(db)
    if not creds:
        print(" [错误] 没有可续期的凭据, 请先登录 (ocbridge login)")
        return 1
    rt = (creds[0].get("value") or {}).get("refresh")
    if not rt:
        print(" [错误] 凭据中没有 refresh token")
        return 1
    try:
        data = _post_json(f"{CONSOLE_BASE}/console/auth/device/token", {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": "opencode-cli",
        })
    except Exception as e:
        print(f" [错误] 续期失败: {e}")
        return 1
    try:
        con = sqlite3.connect(db, timeout=15)
        row = con.execute(
            "SELECT id, value FROM credential WHERE integration_id='opencode' AND active=1").fetchone()
        cid, old_val = row
        old = json.loads(old_val)
        meta = old.get("metadata") or {}
        if data.get("org_id"):
            meta["orgID"] = data["org_id"]
        now = int(time.time() * 1000)
        new_value = {
            "type": "oauth",
            "methodID": old.get("methodID") or "device",
            "refresh": data.get("refresh_token"),
            "access": data.get("access_token"),
            "expires": now + int(data.get("expires_in") or 2592000) * 1000,
            "metadata": meta,
        }
        con.execute("UPDATE credential SET value=?, time_updated=? WHERE id=?",
                    (json.dumps(new_value, ensure_ascii=False), now, cid))
        con.commit()
        con.close()
        exp_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(new_value["expires"] / 1000))
        print(f" [成功] 已续期, 新到期时间: {exp_s}")
        return 0
    except Exception as e:
        print(f" [错误] 写回失败: {e}")
        return 1


def _port_busy(port, host="127.0.0.1"):
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def cmd_serve(cfg, port_override=None):
    print("=" * 62)
    print(f" OpenCode Free Bridge v{VERSION}")
    print("=" * 62)
    host = cfg.get("host", "127.0.0.1")
    port = port_override or int(cfg.get("port", 8899))

    # 端口占用预检 (防止重复启动/端口冲突)
    if _port_busy(port, host):
        try:
            req = urllib.request.Request(f"http://{host}:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                health = json.loads(r.read().decode("utf-8"))
            if "ok" in health:
                print(f" [提示] 端口 {port} 已有一个桥实例在运行 (v{health.get('version', '?')}), 无需重复启动。")
                print("        如需重启: 结束占用该端口的进程后重新运行本程序。")
                return
        except Exception:
            pass
        print(f" [错误] 端口 {port} 已被其他程序占用。")
        print("        请修改 config.json 中的 port, 或结束占用端口的程序后重试。")
        return

    ok, msg = check_login()
    print(f" 登录状态   : {'OK' if ok else '未登录(警告)'} — {msg}")
    backend = BackendManager(cfg)
    atexit.register(backend.shutdown)

    cache = SessionCache(cfg.get("session_cache_size", 100), busy=set())
    Handler.cfg = cfg
    Handler.backend = backend
    Handler.cache = cache
    Handler.busy = cache.busy

    print(f" 后端服务   : 127.0.0.1:{backend.port} (懒启动, 空闲 {backend.idle_timeout}s 自动退出)")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f" OpenAI 接口: http://{host}:{port}/v1/chat/completions")
    print(f" 健康检查   : http://{host}:{port}/health")
    print(f" 模型导入   : python bridge.py sync")
    print(" 按 Ctrl+C 退出\n")

    def on_sig(sig, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, on_sig)
        signal.signal(signal.SIGTERM, on_sig)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 正在清理...")
    finally:
        backend.shutdown()


def main():
    parser = argparse.ArgumentParser(description=f"OpenCode Free Bridge v{VERSION}")
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="启动桥服务 (默认, 双击本程序等价)")
    p_serve.add_argument("--port", type=int, default=None)
    sub.add_parser("sync", help="扫描免费模型并导入 WorkBuddy").add_argument(
        "--fast", action="store_true", help="跳过可用性探测")
    sub.add_parser("uninstall", help="从 WorkBuddy 移除本桥导入的条目")
    sub.add_parser("status", help="检查环境状态")
    p_exp = sub.add_parser("export-auth", help="提取 opencode 登录状态 (换电脑用)")
    p_exp.add_argument("-o", "--output", default=None, help="输出文件 (默认 ocbridge-auth.json)")
    p_exp.add_argument("--db", default=None, help="指定 opencode.db 路径 (默认本机)")
    p_imp = sub.add_parser("import-auth", help="导入 opencode 登录状态 (换电脑用)")
    p_imp.add_argument("file", help="export-auth 导出的 JSON 文件")
    p_imp.add_argument("--data-dir", default=None, help="目标 opencode 数据目录 (默认为本机)")
    p_setup = sub.add_parser("setup", help="环境准备/自检")
    p_setup.add_argument("--download", action="store_true", help="自动下载 opencode 二进制")
    sub.add_parser("login", help="登录 OpenCode (device flow, 无需安装客户端)")
    sub.add_parser("refresh-auth", help="续期登录凭据 (30 天)")
    args = parser.parse_args()

    cfg = load_config()
    CFG_REF[0] = cfg
    cmd = args.cmd or "serve"
    if cmd == "serve":
        cmd_serve(cfg, getattr(args, "port", None))
    elif cmd == "sync":
        sys.exit(cmd_sync(cfg, fast=getattr(args, "fast", False)))
    elif cmd == "uninstall":
        sys.exit(cmd_uninstall(cfg))
    elif cmd == "status":
        cmd_status(cfg)
    elif cmd == "export-auth":
        sys.exit(cmd_export_auth(cfg, args))
    elif cmd == "import-auth":
        sys.exit(cmd_import_auth(cfg, args))
    elif cmd == "setup":
        sys.exit(cmd_setup(cfg, args))
    elif cmd == "login":
        sys.exit(cmd_login(cfg, args))
    elif cmd == "refresh-auth":
        sys.exit(cmd_refresh_auth(cfg, args))


if __name__ == "__main__":
    main()
