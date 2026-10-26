# -*- coding: utf-8 -*-
"""
svc_sftpserver.py —— 将 SFTP 服务器工具安装为 Windows 服务

用法（管理员 PowerShell / 命令行）：
  python svc_sftpserver.py install     # 安装服务
  python svc_sftpserver.py start       # 启动服务
  python svc_sftpserver.py stop        # 停止服务
  python svc_sftpserver.py remove      # 卸载服务

服务启动后：SFTP 服务（默认 0.0.0.0:2222）与本地管理界面（127.0.0.1:18080）
随系统自动运行，无需登录。
"""

import os
import sys
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import servicemanager
import win32event
import win32service
import win32serviceutil

import web_app


class SftpServerToolService(win32serviceutil.ServiceFramework):
    _svc_name_ = "SftpServerTool"
    _svc_display_name_ = "SFTP Server Tool"
    _svc_description_ = "Windows 图形化 SFTP 服务器管理工具：浏览器管理界面 + SFTP 服务（随系统启动）"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._httpd = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)

    def SvcShutdown(self):
        self.SvcStop()

    def SvcRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        self.main()

    def main(self):
        try:
            web_app.init_users()
        except RuntimeError as e:
            servicemanager.LogErrorMsg("init users failed: %s" % e)
            return
        # 1) 自动启动 SFTP 服务
        cfg = web_app.load_config()
        try:
            srv = web_app.start_sftp_server(
                cfg.get("host") or "0.0.0.0",
                int(cfg.get("port") or 2222),
                cfg.get("key_file") or "host_key",
            )
            web_app._log_cb("服务模式：SFTP 服务已自动启动，监听 {}:{}".format(srv.host, srv.port))
        except Exception as e:
            servicemanager.LogErrorMsg("sftp auto start failed: %s" % e)
        # 2) 启动本地管理界面（127.0.0.1:18080）
        try:
            port = 18080
            self._httpd = web_app.ThreadingHTTPServer(("127.0.0.1", port), web_app.Handler)
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        except OSError as e:
            servicemanager.LogErrorMsg("web ui start failed: %s" % e)
        # 3) 等待停止信号
        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
        # 4) 停止清理
        with web_app._server_lock:
            if web_app._server is not None and web_app._server.is_running:
                web_app._server.stop()
        if self._httpd is not None:
            self._httpd.shutdown()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(SftpServerToolService)
