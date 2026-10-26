# -*- coding: utf-8 -*-
"""
app.py —— Windows 图形化 SFTP 服务器管理工具（tkinter 界面）
==============================================================
功能：服务启动/停止、监听地址与端口配置、主机密钥管理、
     用户增删改（用户名/密码/根目录/读写权限/启用状态）、运行日志。

运行：python app.py
"""

import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.messagebox as mb
import tkinter.font as tkfont
from tkinter import filedialog, ttk

from sftp_core import User, UserManager, SftpServer, generate_host_key

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
USERS_PATH = os.path.join(APP_DIR, "users.json")

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 2222,
    "key_file": "host_key",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        mb.showerror("错误", "保存配置失败：{}".format(e))


def abs_path_of(name):
    return name if os.path.isabs(name) else os.path.join(APP_DIR, name)


# ----------------------------------------------------------------------------
# 用户编辑对话框
# ----------------------------------------------------------------------------
class UserDialog(tk.Toplevel):
    def __init__(self, master, users, user=None):
        super().__init__(master)
        self.users = users
        self.editing = user
        self.result = None  # dict 或 None
        self.title("编辑用户" if user else "添加用户")
        self.resizable(False, False)
        self.transient(master)

        pad = {"padx": 8, "pady": 4}
        body = ttk.Frame(self, padding=12)
        body.grid(row=0, column=0, sticky="nsew")

        # 用户名
        ttk.Label(body, text="用户名 *").grid(row=0, column=0, sticky="w", **pad)
        self.var_user = tk.StringVar()
        ent_user = ttk.Entry(body, textvariable=self.var_user, width=34)
        ent_user.grid(row=0, column=1, columnspan=3, sticky="we", **pad)
        if user:
            self.var_user.set(user.username)
            ent_user.configure(state="disabled")

        # 密码（编辑时留空表示不修改）
        ttk.Label(body, text="密码 *").grid(row=1, column=0, sticky="w", **pad)
        self.var_pw = tk.StringVar()
        self.ent_pw = ttk.Entry(body, textvariable=self.var_pw, width=34, show="*")
        self.ent_pw.grid(row=1, column=1, columnspan=3, sticky="we", **pad)

        # 确认密码
        ttk.Label(body, text="确认密码 *").grid(row=2, column=0, sticky="w", **pad)
        self.var_pw2 = tk.StringVar()
        self.ent_pw2 = ttk.Entry(body, textvariable=self.var_pw2, width=34, show="*")
        self.ent_pw2.grid(row=2, column=1, columnspan=3, sticky="we", **pad)

        # 根目录
        ttk.Label(body, text="根目录 *").grid(row=3, column=0, sticky="w", **pad)
        self.var_root = tk.StringVar()
        if user:
            self.var_root.set(user.root)
        ttk.Entry(body, textvariable=self.var_root, width=28).grid(
            row=3, column=1, columnspan=2, sticky="we", **pad)
        ttk.Button(body, text="浏览...", width=6, command=self._browse_root).grid(
            row=3, column=3, sticky="e", **pad)

        # 权限
        ttk.Label(body, text="权限").grid(row=4, column=0, sticky="w", **pad)
        self.var_perm = tk.StringVar(value=(
            user.permission if user else User.PERM_READWRITE))
        ttk.Combobox(body, textvariable=self.var_perm, width=20,
                     state="readonly",
                     values=(User.PERM_READWRITE, User.PERM_READ)).grid(
            row=4, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(body, text="readwrite=读写  readonly=只读", foreground="#666").grid(
            row=5, column=1, columnspan=3, sticky="w", padx=8)

        # 启用 / 自动创建目录
        self.var_enabled = tk.BooleanVar(value=user.enabled if user else True)
        ttk.Checkbutton(body, text="启用该用户",
                        variable=self.var_enabled).grid(
            row=6, column=1, columnspan=3, sticky="w", **pad)
        self.var_mkdir = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="根目录不存在时自动创建",
                        variable=self.var_mkdir).grid(
            row=7, column=1, columnspan=3, sticky="w", **pad)

        # 备注
        ttk.Label(body, text="备注").grid(row=8, column=0, sticky="w", **pad)
        self.var_note = tk.StringVar(value=user.note if user else "")
        ttk.Entry(body, textvariable=self.var_note, width=34).grid(
            row=8, column=1, columnspan=3, sticky="we", **pad)

        # 按钮
        btns = ttk.Frame(self, padding=12)
        btns.grid(row=1, column=0, sticky="e")
        ttk.Button(btns, text="确定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_set()
        if not user:
            ent_user.focus_set()
        else:
            self.ent_pw.focus_set()

    def _browse_root(self):
        d = filedialog.askdirectory(parent=self, title="选择用户根目录")
        if d:
            self.var_root.set(d)

    def _ok(self):
        username = self.var_user.get().strip()
        pw = self.var_pw.get()
        pw2 = self.var_pw2.get()
        root = self.var_root.get().strip()
        perm = self.var_perm.get()
        enabled = self.var_enabled.get()
        mkdir = self.var_mkdir.get()
        note = self.var_note.get().strip()

        if not username:
            mb.showwarning("提示", "请输入用户名", parent=self)
            return
        if not self.editing and self.users.exists(username):
            mb.showwarning("提示", "用户名 {} 已存在".format(username), parent=self)
            return
        if not self.editing and not pw:
            mb.showwarning("提示", "请输入密码", parent=self)
            return
        if pw != pw2:
            mb.showwarning("提示", "两次输入的密码不一致", parent=self)
            return
        if not root:
            mb.showwarning("提示", "请输入用户根目录", parent=self)
            return
        if not os.path.isabs(root):
            mb.showwarning("提示", "根目录必须是绝对路径（如 D:\\sftp\\user1）", parent=self)
            return
        if mkdir and not os.path.exists(root):
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as e:
                mb.showerror("错误", "创建根目录失败：{}".format(e), parent=self)
                return
        if not os.path.exists(root):
            mb.showwarning("提示", "根目录不存在：{}".format(root), parent=self)
            return

        self.result = {
            "username": username,
            "new_password": pw or None,
            "root": root,
            "permission": perm,
            "enabled": enabled,
            "note": note,
        }
        self.destroy()


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SFTP 服务器管理工具")
        self.geometry("960x640")
        self.minsize(820, 560)

        self.cfg = load_config()
        self.users = UserManager(USERS_PATH)
        try:
            self.users.load()
        except RuntimeError as e:
            mb.showerror("错误", str(e))
            sys.exit(1)

        self.server = None
        self.log_q = queue.Queue()

        self._build_ui()
        self._refresh_users()
        self.after(200, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 界面 ----------------
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # --- 服务器设置 ---
        box = ttk.LabelFrame(root, text="服务器设置", padding=8)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        box.columnconfigure(3, weight=1)

        ttk.Label(box, text="监听地址").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.var_host = tk.StringVar(value=self.cfg.get("host", "0.0.0.0"))
        ttk.Entry(box, textvariable=self.var_host, width=16).grid(
            row=0, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(box, text="端口").grid(row=0, column=2, sticky="w", padx=4, pady=3)
        self.var_port = tk.IntVar(value=int(self.cfg.get("port", 2222)))
        ttk.Spinbox(box, from_=1, to=65535, width=8,
                    textvariable=self.var_port).grid(
            row=0, column=3, sticky="w", padx=4, pady=3)

        ttk.Label(box, text="主机密钥").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.var_key = tk.StringVar(value=self.cfg.get("key_file", "host_key"))
        ttk.Entry(box, textvariable=self.var_key).grid(
            row=1, column=1, columnspan=2, sticky="we", padx=4, pady=3)
        ttk.Button(box, text="浏览", width=5,
                   command=self._browse_key).grid(row=1, column=3, sticky="w", padx=4)
        ttk.Button(box, text="重新生成密钥", width=12,
                   command=self._regenerate_key).grid(row=1, column=4, padx=4)

        ops = ttk.Frame(box)
        ops.grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.btn_start = ttk.Button(ops, text="启动服务", command=self._start)
        self.btn_start.pack(side="left", padx=(4, 8))
        self.btn_stop = ttk.Button(ops, text="停止服务", command=self._stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(ops, text="● 未启动", foreground="#999")
        self.lbl_status.pack(side="left", padx=12)

        # --- 用户管理 ---
        ubox = ttk.LabelFrame(root, text="用户管理", padding=8)
        ubox.grid(row=1, column=0, sticky="nsew")
        ubox.rowconfigure(0, weight=1)
        ubox.columnconfigure(0, weight=1)

        cols = ("username", "root", "permission", "status", "note")
        self.tree = ttk.Treeview(ubox, columns=cols, show="headings", height=9)
        self.tree.heading("username", text="用户名")
        self.tree.heading("root", text="根目录")
        self.tree.heading("permission", text="权限")
        self.tree.heading("status", text="状态")
        self.tree.heading("note", text="备注")
        self.tree.column("username", width=120, anchor="w")
        self.tree.column("root", width=330, anchor="w")
        self.tree.column("permission", width=90, anchor="center")
        self.tree.column("status", width=70, anchor="center")
        self.tree.column("note", width=180, anchor="w")
        vsb = ttk.Scrollbar(ubox, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda e: self._edit_user())
        self.tree.bind("<Delete>", lambda e: self._delete_user())

        btns = ttk.Frame(ubox)
        btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(btns, text="添加用户", command=self._add_user).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns, text="编辑", command=self._edit_user).pack(
            side="left", padx=6)
        ttk.Button(btns, text="删除", command=self._delete_user).pack(
            side="left", padx=6)
        ttk.Label(btns, text="（双击行或选中后编辑 / 按 Delete 删除）",
                  foreground="#888").pack(side="left", padx=10)

        # --- 运行日志 ---
        lbox = ttk.LabelFrame(root, text="运行日志", padding=8)
        lbox.grid(row=2, column=0, sticky="ew")
        lbox.rowconfigure(0, weight=1)
        lbox.columnconfigure(0, weight=1)

        self.txt_log = tk.Text(lbox, height=8, wrap="word", state="disabled",
                               font=tkfont.nametofont("TkFixedFont"))
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        s2 = ttk.Scrollbar(lbox, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=s2.set)
        s2.grid(row=0, column=1, sticky="ns")
        self.txt_log.tag_configure("err", foreground="#c0392b")
        self.txt_log.tag_configure("ok", foreground="#1e8449")

    # ---------------- 服务控制 ----------------
    def _browse_key(self):
        f = filedialog.asksaveasfilename(
            parent=self, title="选择主机密钥文件", defaultextension="",
            initialfile=self.var_key.get())
        if f:
            self.var_key.set(os.path.basename(f) if os.path.dirname(f) == APP_DIR
                             else f)

    def _regenerate_key(self):
        if self.server and self.server.is_running:
            mb.showwarning("提示", "请先停止服务再重新生成密钥")
            return
        key_abs = abs_path_of(self.var_key.get())
        if os.path.exists(key_abs) and not mb.askyesno(
                "确认", "将覆盖现有密钥文件：\n{}\n继续？".format(key_abs)):
            return
        try:
            generate_host_key(key_abs)
        except Exception as e:
            mb.showerror("错误", "生成密钥失败：{}".format(e))
            return
        self._log_line("已生成新的主机密钥：{}".format(key_abs), "ok")

    def _start(self):
        host = self.var_host.get().strip()
        port = self.var_port.get()
        key_abs = abs_path_of(self.var_key.get())
        if not host:
            mb.showwarning("提示", "请输入监听地址")
            return
        try:
            int(port)
        except (TypeError, ValueError):
            mb.showwarning("提示", "端口必须为数字")
            return

        if not os.path.isabs(self.var_key.get()):
            self.cfg["key_file"] = self.var_key.get()
        else:
            self.cfg["key_file"] = key_abs
        self.cfg["host"] = host
        self.cfg["port"] = int(port)
        save_config(self.cfg)

        if not os.path.exists(os.path.dirname(key_abs) or APP_DIR):
            mb.showerror("错误", "密钥目录不存在")
            return

        try:
            self.server = SftpServer(host, int(port), key_abs, self.users,
                                     log=self._log_cb)
            self.server.start()
        except Exception as e:
            self.server = None
            mb.showerror("错误", "启动失败：{}".format(e))
            return

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="● 运行中  {}:{}".format(host, port),
                                  foreground="#1e8449")

    def _stop(self):
        if self.server:
            self.server.stop()
            self.server = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl_status.configure(text="● 未启动", foreground="#999")

    # ---------------- 用户管理 ----------------
    def _selected_user(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")[0]

    def _refresh_users(self):
        self.tree.delete(*self.tree.get_children())
        for u in self.users.all():
            perm = "读写" if u.can_write() else "只读"
            status = "启用" if u.enabled else "禁用"
            self.tree.insert("", "end", iid=u.username, values=(
                u.username, u.root, perm, status, u.note))

    def _add_user(self):
        dlg = UserDialog(self, self.users)
        self.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        try:
            self.users.add_user(
                username=r["username"],
                password=r["new_password"],
                root=r["root"],
                permission=r["permission"],
                enabled=r["enabled"],
                note=r["note"],
                auto_create_dir=True,
            )
        except ValueError as e:
            mb.showerror("错误", str(e))
            return
        self._log_line("已添加用户：{}（根目录 {}）".format(
            r["username"], r["root"]), "ok")
        self._refresh_users()

    def _edit_user(self):
        name = self._selected_user()
        if not name:
            mb.showinfo("提示", "请先选择一个用户")
            return
        user = self.users.get(name)
        if not user:
            return
        dlg = UserDialog(self, self.users, user=user)
        self.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        try:
            self.users.update_user(
                username=name,
                new_password=r["new_password"],
                root=r["root"],
                permission=r["permission"],
                enabled=r["enabled"],
                note=r["note"],
                auto_create_dir=True,
            )
        except ValueError as e:
            mb.showerror("错误", str(e))
            return
        self._log_line("已更新用户：{}".format(name), "ok")
        self._refresh_users()

    def _delete_user(self):
        name = self._selected_user()
        if not name:
            mb.showinfo("提示", "请先选择一个用户")
            return
        if not mb.askyesno("确认", "确定删除用户 {} 吗？\n（根目录中的文件不会删除）".format(name)):
            return
        try:
            self.users.delete_user(name)
        except ValueError as e:
            mb.showerror("错误", str(e))
            return
        self._log_line("已删除用户：{}".format(name))
        self._refresh_users()

    # ---------------- 日志 ----------------
    def _log_cb(self, msg):
        self.log_q.put(msg)

    def _log_line(self, msg, tag=None):
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", "[{}] {}\n".format(ts, msg), tag or ())
        if int(self.txt_log.index("end-1c").split(".")[0]) > 1000:
            self.txt_log.delete("1.0", "500.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _drain_logs(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self._log_line(msg)
        except queue.Empty:
            pass
        self.after(200, self._drain_logs)

    # ---------------- 退出 ----------------
    def _on_close(self):
        if self.server and self.server.is_running:
            if not mb.askyesno("确认", "服务仍在运行，确定退出并停止服务吗？"):
                return
            self._stop()
        self.destroy()


def main():
    if "--smoke" in sys.argv:  # 自检模式：构建窗口后自动关闭
        app = App()
        app.after(1200, app.destroy)
        app.mainloop()
        print("GUI smoke test passed")
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    try:  # 高 DPI 清晰显示
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    main()

