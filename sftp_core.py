# -*- coding: utf-8 -*-
"""
sftp_core.py —— Windows 图形化 SFTP 服务器核心模块
===================================================
功能：
  1. 用户管理（用户名/密码/根目录/读写权限/启用状态），密码以 PBKDF2-SHA256 加盐哈希存储，JSON 持久化
  2. 基于 paramiko 的 SFTP 服务端：密码认证、按用户 chroot 目录隔离、读写权限控制
  3. 服务启动 / 停止，多客户端并发连接

依赖：paramiko（pip install paramiko）
"""

import datetime
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading

import paramiko
from paramiko import ServerInterface, Transport
from paramiko.sftp import (
    SFTP_FAILURE,
    SFTP_NO_SUCH_FILE,
    SFTP_OK,
    SFTP_OP_UNSUPPORTED,
    SFTP_PERMISSION_DENIED,
)
from paramiko.sftp_attr import SFTPAttributes
from paramiko.sftp_handle import SFTPHandle
from paramiko.sftp_server import SFTPServer
from paramiko.sftp_si import SFTPServerInterface

# ----------------------------------------------------------------------------
# 密码哈希：PBKDF2-SHA256，格式 pbkdf2_sha256$迭代次数$salt(hex)$hash(hex)
# ----------------------------------------------------------------------------
_PBKDF2_ITERATIONS = 210_000


def hash_password(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ----------------------------------------------------------------------------
# 用户模型
# ----------------------------------------------------------------------------
class User:
    PERM_READ = "readonly"
    PERM_READWRITE = "readwrite"

    def __init__(self, username, password_hash, root,
                 permission=PERM_READWRITE, enabled=True, note=""):
        self.username = username
        self.password_hash = password_hash
        self.root = root
        self.permission = permission
        self.enabled = enabled
        self.note = note

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.password_hash)

    def can_write(self) -> bool:
        return self.permission == self.PERM_READWRITE

    def to_dict(self):
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "root": self.root,
            "permission": self.permission,
            "enabled": self.enabled,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            username=str(d.get("username", "")),
            password_hash=str(d.get("password_hash", "")),
            root=str(d.get("root", "")),
            permission=d.get("permission", cls.PERM_READWRITE),
            enabled=bool(d.get("enabled", True)),
            note=str(d.get("note", "")),
        )


# ----------------------------------------------------------------------------
# 用户管理：JSON 文件持久化
# ----------------------------------------------------------------------------
class UserManager:
    """用户管理：JSON 文件持久化。线程安全（服务运行时界面修改即时生效）。"""

    def __init__(self, path):
        self.path = path
        self.users = {}  # username -> User
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            self.users.clear()
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                raise RuntimeError("读取用户配置文件失败: {}".format(e))
            for d in data.get("users", []):
                try:
                    u = User.from_dict(d)
                except Exception:
                    continue
                if u.username:
                    self.users[u.username] = u

    def save(self):
        with self._lock:
            payload = {"users": [u.to_dict() for u in self.users.values()]}
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                raise RuntimeError("保存用户配置文件失败: {}".format(e))

    def get(self, username):
        with self._lock:
            return self.users.get(username)

    def all(self):
        with self._lock:
            return list(self.users.values())

    def exists(self, username):
        with self._lock:
            return username in self.users

    def add_user(self, username, password, root, permission, enabled=True,
                 note="", auto_create_dir=False):
        """新增用户；密码为明文，内部转为哈希存储。"""
        with self._lock:
            if not username or not username.strip():
                raise ValueError("用户名不能为空")
            if self.exists(username):
                raise ValueError("用户名 {} 已存在".format(username))
            if not password:
                raise ValueError("密码不能为空")
            if not root or not os.path.isabs(root):
                raise ValueError("根目录必须是绝对路径")
            if permission not in (User.PERM_READ, User.PERM_READWRITE):
                raise ValueError("权限取值无效")
            if auto_create_dir and not os.path.exists(root):
                try:
                    os.makedirs(root, exist_ok=True)
                except OSError as e:
                    raise ValueError("创建根目录失败: {}".format(e))
            if not os.path.exists(root):
                raise ValueError("根目录不存在：{}".format(root))
            u = User(
                username.strip(),
                hash_password(password),
                os.path.normpath(root),
                permission,
                enabled,
                note,
            )
            self.users[u.username] = u
            self.save()
            return u

    def update_user(self, username, new_password=None, root=None,
                    permission=None, enabled=None, note=None,
                    auto_create_dir=False):
        with self._lock:
            u = self.get(username)
            if u is None:
                raise ValueError("用户 {} 不存在".format(username))
            if new_password:
                u.password_hash = hash_password(new_password)
            if root is not None:
                if not os.path.isabs(root):
                    raise ValueError("根目录必须是绝对路径")
                if auto_create_dir and not os.path.exists(root):
                    os.makedirs(root, exist_ok=True)
                if not os.path.exists(root):
                    raise ValueError("根目录不存在：{}".format(root))
                u.root = os.path.normpath(root)
            if permission is not None:
                if permission not in (User.PERM_READ, User.PERM_READWRITE):
                    raise ValueError("权限取值无效")
                u.permission = permission
            if enabled is not None:
                u.enabled = bool(enabled)
            if note is not None:
                u.note = note
            self.save()
            return u

    def delete_user(self, username):
        with self._lock:
            if username not in self.users:
                raise ValueError("用户 {} 不存在".format(username))
            del self.users[username]
            self.save()


# ----------------------------------------------------------------------------
# 认证接口
# ----------------------------------------------------------------------------
class AuthServer(ServerInterface):
    """处理 SSH/SFTP 客户端的认证请求。

    注意（paramiko 5.x）：不要重写 check_channel_subsystem_request —— 默认
    实现会从 subsystem_table 取出已注册的处理器并启动它；重写会导致 SFTP
    子系统永远不会被处理。
    """

    def __init__(self, transport, users):
        super().__init__()
        self.transport = transport
        self.users = users
        self.auth_user = None  # 认证成功后挂载的用户，供 SFTP 处理器读取

    def check_auth_password(self, username, password):
        u = self.users.get(username)
        if u is not None and u.enabled and u.check_password(password):
            self.auth_user = u
            self.transport.auth_user = u
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel):
        return False

    def check_channel_exec_request(self, channel, command):
        return False


# ----------------------------------------------------------------------------
# 文件工具
# ----------------------------------------------------------------------------
def _open_with_flags(path, flags):
    """用 int flags 打开文件（Python 内置 open() 的 mode 不接受 int，
    需 os.open 获得 fd 后再 os.fdopen 包成文件对象）。"""
    fd = os.open(path, flags, 0o666)
    if flags & os.O_RDWR:
        mode = "ab" if flags & os.O_APPEND else "r+b"
    elif flags & os.O_WRONLY:
        mode = "ab" if flags & os.O_APPEND else "wb"
    else:
        mode = "rb"
    return os.fdopen(fd, mode)


# ----------------------------------------------------------------------------
# SFTP 文件系统实现：chroot + 读写权限
# ----------------------------------------------------------------------------
class WinSFTPHandle(SFTPHandle):
    """包装本地文件对象的 SFTP 句柄（paramiko 5.x 需要 readfile/writefile 属性）。"""

    def __init__(self, f, flags):
        super().__init__(flags=flags)
        self.readfile = f
        self.writefile = f

    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        # Windows 下仅尝试应用只读属性位，其余忽略
        try:
            if attr.st_mode is not None:
                os.chmod(self.readfile.fileno(), attr.st_mode)
            return SFTP_OK
        except (OSError, NotImplementedError):
            return SFTP_OP_UNSUPPORTED


class ChrootSFTP(SFTPServerInterface):
    """每个登录用户被限制在各自根目录内（chroot），并受读写权限约束。"""

    def __init__(self, server, *args, **kwargs):
        super().__init__(server)
        self.user = getattr(server, "auth_user", None)
        if self.user is None:
            self.root = os.getcwd()
            self.readonly = True
        else:
            self.root = os.path.normpath(os.path.abspath(self.user.root))
            self.readonly = not self.user.can_write()
        self.root_norm = os.path.normcase(self.root)

    # ---- 内部工具 ----
    def _real(self, path):
        """虚拟路径（/ 开头）-> 用户根目录下的实际路径；越界路径抛 OSError。"""
        p = path.replace("/", os.sep)
        if p.startswith(os.sep):
            p = p[len(os.sep):]
        if not p:
            return self.root
        full = os.path.normpath(os.path.join(self.root, p))
        full_norm = os.path.normcase(full)
        if full_norm == self.root_norm or full_norm.startswith(self.root_norm + os.sep):
            return full
        raise OSError(13, "Access denied: path escapes root")

    def _virtual(self, full):
        """实际路径 -> 虚拟路径。"""
        rel = os.path.relpath(full, self.root)
        if rel == ".":
            return "/"
        return "/" + rel.replace(os.sep, "/")

    def _is_write(self, flags):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT
                             | os.O_TRUNC | os.O_APPEND))

    # ---- 必须实现的 SFTPServerInterface 方法 ----
    def canonicalize(self, path):
        try:
            return self._virtual(self._real(path))
        except OSError:
            return "/"

    def open(self, path, flags, attr):
        if self.readonly and self._is_write(flags):
            return SFTP_PERMISSION_DENIED
        try:
            full = self._real(path)
        except OSError:
            return SFTP_NO_SUCH_FILE
        try:
            f = _open_with_flags(full, flags)
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except IsADirectoryError:
            return SFTP_FAILURE
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return WinSFTPHandle(f, flags)

    def list_folder(self, path):
        try:
            full = self._real(path)
            names = os.listdir(full)
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        out = []
        for n in names:
            try:
                st = os.stat(os.path.join(full, n))
                a = SFTPAttributes.from_stat(st)
                a.filename = n
                out.append(a)
            except OSError:
                continue
        return out

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def remove(self, path):
        if self.readonly:
            return SFTP_PERMISSION_DENIED
        try:
            os.remove(self._real(path))
            return SFTP_OK
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def rename(self, oldpath, newpath):
        if self.readonly:
            return SFTP_PERMISSION_DENIED
        try:
            os.rename(self._real(oldpath), self._real(newpath))
            return SFTP_OK
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        if self.readonly:
            return SFTP_PERMISSION_DENIED
        try:
            os.mkdir(self._real(path))
            return SFTP_OK
        except FileExistsError:
            return SFTP_FAILURE
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def rmdir(self, path):
        if self.readonly:
            return SFTP_PERMISSION_DENIED
        try:
            os.rmdir(self._real(path))
            return SFTP_OK
        except FileNotFoundError:
            return SFTP_NO_SUCH_FILE
        except PermissionError:
            return SFTP_PERMISSION_DENIED
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def symlink(self, target_path, path):
        if self.readonly:
            return SFTP_PERMISSION_DENIED
        try:
            os.symlink(target_path, self._real(path))
            return SFTP_OK
        except (OSError, NotImplementedError):
            return SFTP_OP_UNSUPPORTED

    def readlink(self, path):
        try:
            return os.readlink(self._real(path))
        except OSError:
            return SFTP_NO_SUCH_FILE

    def chattr(self, path, attr):
        if self.readonly:
            return SFTP_PERMISSION_DENIED
        try:
            if attr.st_mode is not None:
                os.chmod(self._real(path), attr.st_mode)
            return SFTP_OK
        except (OSError, NotImplementedError):
            return SFTP_OP_UNSUPPORTED


# ----------------------------------------------------------------------------
# SFTP 服务器：监听 socket + 并发处理连接
# ----------------------------------------------------------------------------
def generate_host_key(path):
    """生成并保存新的主机密钥（paramiko 5.x：RSA 2048，生成约 0.1 秒）。"""
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(path)
    return key


class SftpServer:
    def __init__(self, host, port, key_path, users, log=None):
        self.host = host
        self.port = int(port)
        self.key_path = key_path
        self.users = users
        self._log_fn = log if log is not None else (lambda msg: None)
        self._running = False
        self._sock = None
        self._thread = None
        self._transports = []
        self._lock = threading.Lock()
        self._key = self._load_or_create_key(key_path)

    # ---- 日志 ----
    def _log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self._log_fn("[{}] {}".format(ts, msg))
        except Exception:
            pass

    # ---- 主机密钥 ----
    @staticmethod
    def _load_or_create_key(path):
        if os.path.exists(path):
            try:
                return paramiko.Ed25519Key.from_private_key_file(path)
            except Exception:
                return paramiko.RSAKey.from_private_key_file(path)
        return generate_host_key(path)

    @property
    def is_running(self):
        return self._running

    def start(self):
        if self._running:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.host, self.port))
            self._sock.listen(20)
        except OSError as e:
            self._sock.close()
            self._sock = None
            raise RuntimeError("绑定 {}:{} 失败: {}".format(self.host, self.port, e))
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True, name="sftp-accept")
        self._thread.start()
        self._log("SFTP 服务已启动，监听 {}:{}（用户 {} 个）".format(
            self.host, self.port, len(self.users.all())))

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            self._log("新连接：{}:{}".format(addr[0], addr[1]))
            threading.Thread(target=self._handle_client, args=(conn, addr),
                             daemon=True, name="sftp-client").start()

    def _handle_client(self, conn, addr):
        t = None
        try:
            t = Transport(conn)
            t.add_server_key(self._key)
            t.set_subsystem_handler("sftp", SFTPServer, sftp_si=ChrootSFTP)
            t.start_server(server=AuthServer(t, self.users))
            with self._lock:
                self._transports.append(t)
            # 保持 transport 活跃，直到客户端断开
            while t.is_active():
                chan = t.accept(30)
                if chan is None:
                    continue
            u = getattr(t, "auth_user", None)
            name = u.username if u else "未知用户"
            self._log("{}:{}（{}）断开连接".format(addr[0], addr[1], name))
        except EOFError:
            pass
        except Exception as e:
            self._log("{}:{} 会话异常: {}".format(addr[0], addr[1], e))
        finally:
            if t is not None:
                with self._lock:
                    if t in self._transports:
                        self._transports.remove(t)
                try:
                    t.close()
                except Exception:
                    pass

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            transports = list(self._transports)
        for t in transports:
            try:
                t.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._log("SFTP 服务已停止")
