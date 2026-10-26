# -*- coding: utf-8 -*-
"""
test_smoke.py —— SFTP 服务器核心功能的自动化冒烟测试
====================================================
验证项：
  1. 错误密码 / 不存在用户被拒绝
  2. 读写用户：上传、下载、建目录、重命名、删除、中文文件名
  3. chroot 隔离：无法访问根目录之外的文件
  4. 只读用户：可读不可写
  5. 服务可正常启动与停止

运行：python test_smoke.py
"""

import os
import shutil
import socket
import sys
import tempfile

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sftp_core import SftpServer, User, UserManager  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print("PASS  " + name)
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, str(e)))
        print("FAIL  {} -> {}".format(name, e))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    base = tempfile.mkdtemp(prefix="sftp_smoke_")
    port = free_port()
    logs = []
    try:
        root_alice = os.path.join(base, "alice")
        root_bob = os.path.join(base, "bob")
        secret = os.path.join(base, "secret.txt")
        os.makedirs(root_alice)
        os.makedirs(root_bob)
        with open(secret, "w", encoding="utf-8") as f:
            f.write("TOP SECRET DATA")

        um = UserManager(os.path.join(base, "users.json"))
        um.add_user("alice", "pw123456", root_alice, User.PERM_READWRITE)
        um.add_user("bob", "pw654321", root_bob, User.PERM_READ)

        srv = SftpServer("127.0.0.1", port, os.path.join(base, "host_key"),
                         um, log=logs.append)
        srv.start()

        def connect(user, pw):
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect("127.0.0.1", port, username=user, password=pw, timeout=10)
            return c

        # 1. 错误密码
        def t_bad_password():
            try:
                connect("alice", "wrong-password")
            except paramiko.AuthenticationException:
                return
            raise AssertionError("错误密码应认证失败")
        check("错误密码被拒绝", t_bad_password)

        # 2. 不存在的用户
        def t_unknown_user():
            try:
                connect("nobody", "pw123456")
            except paramiko.AuthenticationException:
                return
            raise AssertionError("不存在用户应认证失败")
        check("不存在用户被拒绝", t_unknown_user)

        # 3. alice 读写：上传/下载/目录操作
        def t_upload_download():
            c = connect("alice", "pw123456")
            try:
                sftp = c.open_sftp()
                assert sftp.normalize("/") == "/", "canonicalize 应返回 /"
                # 上传
                local_src = os.path.join(base, "hello.txt")
                with open(local_src, "w", encoding="utf-8") as f:
                    f.write("hello sftp world")
                sftp.put(local_src, "/hello.txt")
                assert os.path.exists(os.path.join(root_alice, "hello.txt"))
                # 下载并比对
                local_dst = os.path.join(base, "hello_dl.txt")
                sftp.get("/hello.txt", local_dst)
                with open(local_dst, "r", encoding="utf-8") as f:
                    assert f.read() == "hello sftp world"
                # 建目录/重命名/删除
                sftp.mkdir("/sub")
                assert os.path.isdir(os.path.join(root_alice, "sub"))
                sftp.rename("/hello.txt", "/sub/hello2.txt")
                assert os.path.exists(os.path.join(root_alice, "sub", "hello2.txt"))
                assert not os.path.exists(os.path.join(root_alice, "hello.txt"))
                listing = sftp.listdir("/sub")  # listdir 返回文件名列表
                assert listing == ["hello2.txt"], "listdir 内容不符: %r" % listing
                sftp.remove("/sub/hello2.txt")
                assert not os.path.exists(os.path.join(root_alice, "sub", "hello2.txt"))
                sftp.rmdir("/sub")
                assert not os.path.exists(os.path.join(root_alice, "sub"))
            finally:
                c.close()
        check("读写用户：上传/下载/目录操作", t_upload_download)

        # 4. 中文文件名
        def t_chinese_name():
            c = connect("alice", "pw123456")
            try:
                sftp = c.open_sftp()
                with sftp.open("/中文文件.txt", "w") as f:
                    f.write("中文内容 OK")
                assert os.path.exists(os.path.join(root_alice, "中文文件.txt"))
                with sftp.open("/中文文件.txt", "r") as f:
                    assert f.read().decode("utf-8") == "中文内容 OK"
                sftp.remove("/中文文件.txt")
            finally:
                c.close()
        check("中文文件名读写", t_chinese_name)

        # 5. chroot 隔离：无法越界访问根目录之外的文件
        def t_chroot_escape():
            c = connect("alice", "pw123456")
            try:
                sftp = c.open_sftp()
                for evil in ("../../secret.txt", "/../secret.txt",
                             "../../../secret.txt"):
                    try:
                        sftp.open(evil, "r")
                        raise AssertionError("越界路径应被拒绝: " + evil)
                    except IOError:
                        pass
                # 根目录列表不应出现根目录之外的 secret.txt
                names = sftp.listdir("/")  # listdir 返回文件名列表
                assert "secret.txt" not in names, "根目录外文件不应可见"
            finally:
                c.close()
        check("chroot 隔离（禁止越界访问）", t_chroot_escape)

        # 5b. 额外验证：根目录外文件确实不存在于 alice 视野
        def t_chroot_file_absent():
            c = connect("alice", "pw123456")
            try:
                sftp = c.open_sftp()
                try:
                    sftp.stat("/../../secret.txt")
                    raise AssertionError("根目录外文件不应可见")
                except IOError:
                    pass
            finally:
                c.close()
        check("根目录外文件不可见", t_chroot_file_absent)

        # 6. 只读用户 bob：可读不可写
        def t_readonly():
            c = connect("bob", "pw654321")
            try:
                sftp = c.open_sftp()
                # 可读：列出根目录
                sftp.listdir("/")
                # 写操作应被拒绝
                for op in (
                    lambda: sftp.open("/nope.txt", "w"),
                    lambda: sftp.mkdir("/newdir"),
                ):
                    try:
                        op()
                        raise AssertionError("只读用户写操作应被拒绝")
                    except (IOError, OSError):
                        pass
                assert not os.path.exists(os.path.join(root_bob, "nope.txt"))
                assert not os.path.exists(os.path.join(root_bob, "newdir"))
            finally:
                c.close()
        check("只读用户：可读不可写", t_readonly)

        # 7. 停止服务
        def t_stop():
            srv.stop()
            assert not srv.is_running
            c = socket.socket()
            c.settimeout(2)
            try:
                c.connect(("127.0.0.1", port))
                raise AssertionError("服务停止后端口仍可连接")
            except OSError:
                pass
            finally:
                c.close()
        check("服务停止后端口关闭", t_stop)

    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("-" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("结果：{} / {} 通过".format(passed, len(RESULTS)))
    if logs:
        print("服务日志（共 {} 条）：".format(len(logs)))
        for line in logs[:12]:
            print("  " + line)
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
