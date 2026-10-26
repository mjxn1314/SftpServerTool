# -*- coding: utf-8 -*-
"""
web_app.py —— SFTP 服务器管理工具（本地 Web 图形界面入口）
==========================================================
系统 Python 环境可能缺少 tkinter 运行时，本入口改用「本地浏览器界面」：
启动一个仅绑定 127.0.0.1 的内置 HTTP 服务（含 REST API + 管理页面），
并自动打开默认浏览器进行操作。

功能与 tkinter 版（app.py）一致：
  服务启停、监听地址/端口/主机密钥管理、用户增删改（密码/根目录/权限）、实时日志。

运行：python web_app.py   （start_gui.bat 已指向本入口）
"""

import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from sftp_core import User, UserManager, SftpServer, generate_host_key

try:
    from ui_embedded import UI_HTML  # 内嵌的管理页面：外部 web_ui.html 缺失时兜底（单文件打包亦可用）
except ImportError:
    UI_HTML = None

if getattr(sys, "frozen", False):
    RES_DIR = sys._MEIPASS                     # 打包进 exe 的只读资源（web_ui.html）
    APP_DIR = os.path.dirname(sys.executable)  # 运行时数据目录（config/users/host_key 持久化于 exe 旁）
else:
    RES_DIR = APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
USERS_PATH = os.path.join(APP_DIR, "users.json")
UI_PATH = os.path.join(RES_DIR, "web_ui.html")

DEFAULT_CONFIG = {"host": "0.0.0.0", "port": 2222, "key_file": "host_key"}
DEFAULT_WEB_PORT = 18080

_token = secrets.token_hex(16)
_log_list = []          # 已消费日志（供 GET /api/logs 增量返回）
_log_seq = 0
_server = None          # SftpServer 实例（运行中才有）
_server_lock = threading.Lock()
_users = UserManager(USERS_PATH)  # 全局共享实例：界面修改即时生效于运行中的服务


# ----------------------------------------------------------------------------
# 配置与用户
# ----------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def abs_path_of(name):
    return name if os.path.isabs(name) else os.path.join(APP_DIR, name)


def init_users():
    """启动时加载用户配置（幂等，可重复调用）。"""
    with _server_lock:
        _users.load()


# ----------------------------------------------------------------------------
# 服务器日志
# ----------------------------------------------------------------------------
def _log_cb(msg):
    global _log_seq
    _log_seq += 1
    _log_list.append((_log_seq, msg))
    if len(_log_list) > 2000:
        del _log_list[:500]


def get_logs(after):
    return [(s, m) for s, m in _log_list if s > after]


# ----------------------------------------------------------------------------
# HTTP 请求处理
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SftpTool/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _check_token(self):
        return self.headers.get("X-Auth-Token") == _token

    def log_message(self, fmt, *args):
        pass  # 抑制默认访问日志，界面日志只记录服务相关

    # ---- 路由 ----
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._serve_ui()
        elif self.path == "/api/status":
            self._api_status()
        elif self.path.startswith("/api/users"):
            self._api_users_get()
        elif self.path.startswith("/api/logs"):
            self._api_logs()
        else:
            self._send_text("Not Found", 404)

    def do_POST(self):
        if not self._check_token():
            self._send_json({"error": "未授权"}, 403)
            return
        if self.path == "/api/start":
            self._api_start()
        elif self.path == "/api/stop":
            self._api_stop()
        elif self.path == "/api/key/regenerate":
            self._api_key_regenerate()
        elif self.path == "/api/users":
            self._api_users_post()
        else:
            self._send_json({"error": "Not Found"}, 404)

    def do_PUT(self):
        if not self._check_token():
            self._send_json({"error": "未授权"}, 403)
            return
        if self.path.startswith("/api/users/"):
            self._api_users_put()
        else:
            self._send_json({"error": "Not Found"}, 404)

    def do_DELETE(self):
        if not self._check_token():
            self._send_json({"error": "未授权"}, 403)
            return
        if self.path.startswith("/api/users/"):
            self._api_users_delete()
        else:
            self._send_json({"error": "Not Found"}, 404)

    def do_OPTIONS(self):
        # 浏览器跨站预检：仅允许本地页面，其余拒绝（防 CSRF）
        origin = self.headers.get("Origin", "")
        if origin in ("null", "http://127.0.0.1", "http://localhost"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        else:
            self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- 页面 ----
    def _serve_ui(self):
        html = None
        try:  # 优先读取外部页面文件（便于直接修改样式）
            with open(UI_PATH, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            pass
        if html is None:
            if UI_HTML is None:
                self._send_text("web_ui.html 缺失且无内嵌页面资源", 500)
                return
            html = UI_HTML
        html = html.replace("__AUTH_TOKEN__", _token)
        self._send_text(html, content_type="text/html; charset=utf-8")

    # ---- API：状态 ----
    def _api_status(self):
        with _server_lock:
            srv = _server
            running = srv is not None and srv.is_running
            host, port = (srv.host, srv.port) if srv else (None, None)
            key = srv.key_path if srv else abs_path_of(load_config().get("key_file", "host_key"))
        um = _users
        self._send_json({
            "running": running,
            "host": host,
            "port": port,
            "key": key,
            "user_count": len(um.all()),
            "config": load_config(),
        })

    # ---- API：启停 ----
    def _api_start(self):
        global _server
        data = self._read_body()
        cfg = load_config()
        host = (data.get("host") or cfg.get("host") or "0.0.0.0").strip()
        try:
            port = int(data.get("port") or cfg.get("port") or 2222)
        except (TypeError, ValueError):
            self._send_json({"error": "端口必须为数字"}, 400)
            return
        key_file = (data.get("key_file") or cfg.get("key_file") or "host_key").strip()

        cfg["host"], cfg["port"], cfg["key_file"] = host, port, key_file
        try:
            save_config(cfg)
        except OSError as e:
            self._send_json({"error": "保存配置失败: {}".format(e)}, 500)
            return

        key_abs = abs_path_of(key_file)
        with _server_lock:
            if _server is not None and _server.is_running:
                self._send_json({"error": "服务已在运行"}, 400)
                return
            try:
                srv = SftpServer(host, port, key_abs, _users, log=_log_cb)
                srv.start()
            except Exception as e:
                self._send_json({"error": "启动失败: {}".format(e)}, 500)
                return
            _server = srv
        self._send_json({"ok": True, "host": host, "port": port})

    def _api_stop(self):
        global _server
        with _server_lock:
            srv = _server
            if srv is None or not srv.is_running:
                self._send_json({"error": "服务未运行"}, 400)
                return
            srv.stop()
            _server = None
        self._send_json({"ok": True})

    def _api_key_regenerate(self):
        data = self._read_body()
        key_file = (data.get("key_file") or load_config().get("key_file") or "host_key").strip()
        key_abs = abs_path_of(key_file)
        with _server_lock:
            if _server is not None and _server.is_running:
                self._send_json({"error": "请先停止服务再重新生成密钥"}, 400)
                return
            try:
                generate_host_key(key_abs)
            except Exception as e:
                self._send_json({"error": "生成密钥失败: {}".format(e)}, 500)
                return
        _log_cb("已生成新的主机密钥：{}".format(key_abs))
        self._send_json({"ok": True, "key": key_abs})

    # ---- API：用户 ----
    def _api_users_get(self):
        um = _users
        users = [{
            "username": u.username,
            "root": u.root,
            "permission": u.permission,
            "enabled": u.enabled,
            "note": u.note,
        } for u in um.all()]
        self._send_json({"users": users})

    def _api_users_post(self):
        d = self._read_body()
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        root = (d.get("root") or "").strip()
        permission = d.get("permission") or User.PERM_READWRITE
        enabled = bool(d.get("enabled", True))
        note = (d.get("note") or "").strip()
        auto_create = bool(d.get("auto_create", True))
        try:
            um = _users
            um.add_user(username, password, root, permission, enabled,
                        note=note, auto_create_dir=auto_create)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        _log_cb("已添加用户：{}（根目录 {}）".format(username, root))
        self._send_json({"ok": True})

    def _api_users_put(self):
        name = unquote(self.path[len("/api/users/"):].split("/")[0])
        d = self._read_body()
        try:
            um = _users
            um.update_user(
                username=name,
                new_password=d.get("new_password") or None,
                root=(d.get("root") or "").strip() or None,
                permission=d.get("permission"),
                enabled=None if d.get("enabled") is None else bool(d.get("enabled")),
                note=d.get("note"),
                auto_create_dir=bool(d.get("auto_create", True)),
            )
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        _log_cb("已更新用户：{}".format(name))
        self._send_json({"ok": True})

    def _api_users_delete(self):
        name = unquote(self.path[len("/api/users/"):].split("/")[0])
        try:
            um = _users
            um.delete_user(name)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        _log_cb("已删除用户：{}".format(name))
        self._send_json({"ok": True})

    # ---- API：日志 ----
    def _api_logs(self):
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        try:
            after = int(qs.get("after", ["0"])[0])
        except ValueError:
            after = 0
        logs = get_logs(after)
        latest = logs[-1][0] if logs else after
        self._send_json({"logs": [m for _, m in logs], "cursor": latest})


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def pick_port(start):
    for p in range(start, start + 50):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    raise RuntimeError("找不到可用端口（18080-18129 均被占用）")


def main():
    try:
        init_users()
    except RuntimeError as e:
        print("错误：{}".format(e))
        sys.exit(1)
    args = sys.argv[1:]
    no_browser = "--no-browser" in args
    port = DEFAULT_WEB_PORT
    try:
        i = args.index("--port")
        port = int(args[i + 1])
    except (ValueError, IndexError):
        port = pick_port(DEFAULT_WEB_PORT)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:{}".format(port)
    print("=" * 56)
    print("  SFTP 服务器管理工具 - 浏览器界面")
    print("  请在浏览器中打开：{}".format(url))
    print("  关闭本窗口即退出管理工具（SFTP 服务会随之停止）")
    print("=" * 56)

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        with _server_lock:
            if _server is not None and _server.is_running:
                _server.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
