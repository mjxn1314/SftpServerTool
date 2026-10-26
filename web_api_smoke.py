# -*- coding: utf-8 -*-
"""
web_api_smoke.py —— Web 管理界面的端到端自动化测试
==================================================
验证项：
  1. 管理页面可访问（GET / 返回 HTML 并注入令牌）
  2. 未带令牌的修改请求被拒绝（403）
  3. 添加用户 / 用户列表 / 更新用户 / 删除用户
  4. 启动 SFTP 服务 -> 状态为运行中
  5. 真实 SFTP 客户端（paramiko）连接并上传下载（端到端）
  6. 停止服务 -> 状态为未运行
测试结束后恢复 config.json / users.json 原状。

运行：python web_api_smoke.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paramiko  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_PORT = 18100
SFTP_PORT = 2225
RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print("PASS  " + name)
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, str(e)))
        print("FAIL  {} -> {}".format(name, e))


def http(method, path, body=None, token=None, expect_status=None):
    url = "http://127.0.0.1:{}{}".format(WEB_PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8")
    if expect_status is not None and status != expect_status:
        raise AssertionError("HTTP {} 期望 {}，实际 {}: {}".format(
            method, expect_status, status, raw[:200]))
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    cfg_backup = os.path.join(APP_DIR, "config.json")
    users_backup = os.path.join(APP_DIR, "users.json")
    saved = {}
    for p in (cfg_backup, users_backup):
        if os.path.exists(p):
            saved[p] = open(p, "rb").read()

    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "web_app.py", "--port", str(WEB_PORT),
             "--no-browser"],
            cwd=APP_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(WEB_PORT):
            raise AssertionError("管理界面端口 {} 未就绪".format(WEB_PORT))

        # 1. 页面可访问 + 提取令牌
        def t_page():
            status, html = http("GET", "/")
            assert status == 200
            assert "SFTP 服务器管理工具" in html
            m = re.search(r'const TOKEN = "([0-9a-f]{32})"', html)
            assert m, "页面未注入令牌"
            return m.group(1)
        token = check_get = None

        def t_page_get():
            nonlocal token
            token = t_page()
        check("管理页面可访问且令牌已注入", t_page_get)

        # 2. 无令牌请求被拒
        def t_no_token():
            http("POST", "/api/users", {"username": "x", "password": "y",
                                        "root": "C:\\"}, expect_status=403)
        check("未带令牌的修改请求被拒绝", t_no_token)

        # 3. 用户增改删
        test_root = os.path.join(tempfile.gettempdir(), "sftp_web_test_user")

        def t_add_user():
            http("POST", "/api/users", {
                "username": "webuser", "password": "pw123456",
                "root": test_root, "permission": "readwrite",
                "enabled": True, "auto_create": True,
            }, token=token, expect_status=200)
        check("添加用户", t_add_user)

        def t_list_users():
            status, data = http("GET", "/api/users", token=token)
            assert status == 200
            names = [u["username"] for u in data["users"]]
            assert "webuser" in names, "用户列表应包含 webuser"
        check("用户列表包含新用户", t_list_users)

        def t_update_user():
            http("PUT", "/api/users/webuser", {
                "permission": "readonly", "note": "test",
            }, token=token, expect_status=200)
            status, data = http("GET", "/api/users", token=token)
            u = next(x for x in data["users"] if x["username"] == "webuser")
            assert u["permission"] == "readonly" and u["note"] == "test"
        check("更新用户（权限改为只读）", t_update_user)

        # 4. 启动 SFTP 服务
        def t_start():
            http("POST", "/api/start", {
                "host": "127.0.0.1", "port": SFTP_PORT, "key_file": "host_key",
            }, token=token, expect_status=200)
            status, data = http("GET", "/api/status", token=token)
            assert data["running"] is True, "服务应处于运行状态"
            assert data["port"] == SFTP_PORT
        check("启动 SFTP 服务", t_start)

        # 5. 端到端：paramiko 客户端上传/下载（服务端权限已改为只读，先改回读写）
        def t_sftp_e2e():
            http("PUT", "/api/users/webuser", {
                "permission": "readwrite",
            }, token=token, expect_status=200)
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect("127.0.0.1", SFTP_PORT, username="webuser",
                      password="pw123456", timeout=10)
            try:
                sftp = c.open_sftp()
                sftp.mkdir("/sub")
                with sftp.open("/sub/hello.txt", "w") as f:
                    f.write("web e2e ok")
                with sftp.open("/sub/hello.txt", "r") as f:
                    assert f.read() == b"web e2e ok"
                assert os.path.exists(os.path.join(test_root, "sub", "hello.txt"))
                sftp.remove("/sub/hello.txt")
                sftp.rmdir("/sub")
            finally:
                c.close()
        check("SFTP 客户端端到端上传下载", t_sftp_e2e)

        # 6. 只读用户写被拒（通过界面设置后验证）
        def t_readonly_deny():
            http("PUT", "/api/users/webuser", {
                "permission": "readonly",
            }, token=token, expect_status=200)
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect("127.0.0.1", SFTP_PORT, username="webuser",
                      password="pw123456", timeout=10)
            try:
                sftp = c.open_sftp()
                try:
                    sftp.open("/deny.txt", "w")
                    raise AssertionError("只读用户写操作应被拒绝")
                except (IOError, OSError):
                    pass
                assert not os.path.exists(os.path.join(test_root, "deny.txt"))
            finally:
                c.close()
        check("界面设置只读后写入被拒绝", t_readonly_deny)

        # 7. 停止服务
        def t_stop():
            http("POST", "/api/stop", token=token, expect_status=200)
            status, data = http("GET", "/api/status", token=token)
            assert data["running"] is False
        check("停止 SFTP 服务", t_stop)

        # 8. 删除用户
        def t_delete_user():
            http("DELETE", "/api/users/webuser", token=token, expect_status=200)
            status, data = http("GET", "/api/users", token=token)
            names = [u["username"] for u in data["users"]]
            assert "webuser" not in names
        check("删除用户", t_delete_user)

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        # 恢复配置
        for p in (cfg_backup, users_backup):
            if p in saved:
                with open(p, "wb") as f:
                    f.write(saved[p])
            elif os.path.exists(p):
                os.remove(p)
        shutil.rmtree(os.path.join(tempfile.gettempdir(), "sftp_web_test_user"),
                      ignore_errors=True)

    print("-" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("结果：{} / {} 通过".format(passed, len(RESULTS)))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
