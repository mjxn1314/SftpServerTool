# SFTP 服务器管理工具（Windows 图形化 / 浏览器界面）

一个运行在 Windows 上的图形化 SFTP 服务器工具：图形界面启动/停止服务，管理用户
（用户名/密码/根目录/读写权限），每个用户被隔离在自己的根目录内。

## 功能

- **服务控制**：一键启动 / 停止 SFTP 服务，可配置监听地址与端口
- **主机密钥**：首次启动自动生成 RSA 2048 主机密钥，支持随时重新生成
- **用户管理**：添加 / 编辑 / 删除用户，设置用户名、密码、根目录、读写权限、启用状态、备注
- **权限目录（chroot）**：每个用户只能访问自己根目录内的文件，越界访问一律拒绝
- **权限控制**：可设置「读写」或「只读」用户，界面修改**即时生效**（无需重启服务）
- **密码安全**：密码以 PBKDF2-SHA256 加盐哈希（21 万次迭代）保存，配置文件中无明文
- **并发连接**：支持多客户端同时连接
- **实时日志**：界面中实时显示连接、断开、传输异常等日志
- **中文文件名**：完整支持

## 环境要求

- Windows 10 / 11，Python 3.8 及以上
- 安装依赖：`pip install paramiko`（界面为标准库自带，无需额外 GUI 库）

## 快速开始

1. 双击 `start_gui.bat`（或命令行执行 `python web_app.py`）
2. 程序会自动打开默认浏览器进入管理界面（仅绑定 127.0.0.1）
3. 在「用户管理」中点击「添加用户」，填写用户名、密码、根目录（可勾选自动创建），选择权限
4. 在「服务设置」中点击「启动服务」
5. 客户端使用任意 SFTP 工具（FileZilla / WinSCP / MobaXterm）连接：
   - 地址：运行机器的 IP 或 `127.0.0.1`
   - 端口：默认 `2222`
   - 用户名 / 密码：刚创建的用户
   - 协议：SFTP / SSH
6. 用完点击「停止服务」；直接关闭命令行窗口也会停止服务并退出工具

## 文件说明

| 文件 | 说明 |
|---|---|
| `web_app.py` | 主程序：内置本地 Web 管理界面 + REST API（浏览器界面） |
| `web_ui.html` | 浏览器管理界面页面 |
| `sftp_core.py` | 服务器核心：用户管理 + paramiko SFTP 服务端 |
| `config.json` | 服务配置：监听地址、端口、密钥文件名 |
| `users.json` | 用户数据（密码为哈希值，非明文） |
| `host_key` | 主机密钥（首次启动自动生成） |
| `app.py` | 备用的 tkinter 版界面（需系统 Python 自带完整 tkinter 运行时） |
| `test_smoke.py` | 核心功能自动化测试（8 项） |
| `web_api_smoke.py` | Web 界面端到端自动化测试（10 项） |
| `start_gui.bat` | 一键启动脚本 |
| `requirements.txt` | Python 依赖 |

## 注意事项

- **防火墙**：如需被局域网其他机器访问，请在 Windows 防火墙放行对应端口，管理员
  PowerShell 执行：
  ```powershell
  netsh advfirewall firewall add rule name="SFTP 2222" dir=in action=allow protocol=TCP localport=2222
  ```
- **端口冲突**：默认端口 2222，避免与系统 OpenSSH 的 22 端口冲突；如启动失败请更换端口。
- **监听地址**：`0.0.0.0` 表示允许所有网卡访问（含局域网）；仅本机使用可填 `127.0.0.1`。
- **管理界面安全**：管理页面只绑定 `127.0.0.1`（本机），修改操作带随机令牌校验，
  其他网站无法越权操作本工具。
- **安全提示**：本工具适用于内网或可信环境的文件共享；如暴露公网请配合防火墙、强密码等防护。

## 测试

```powershell
python test_smoke.py      # 核心功能：认证、上传下载、chroot、只读限制、启停（8 项）
python web_api_smoke.py   # Web 界面：页面/令牌/用户管理/启停/端到端（10 项）
```

## 安装为 Windows 服务（开机自启、后台常驻）

将工具注册为系统服务后，SFTP 服务与管理界面随系统启动，无需登录、无需打开窗口。
以管理员身份打开 PowerShell（或命令行），在源码目录执行：

```powershell
pip install pywin32
python svc_sftpserver.py install     # 安装服务
python svc_sftpserver.py start       # 启动服务
python svc_sftpserver.py stop        # 停止服务
python svc_sftpserver.py remove      # 卸载服务
```

- 服务名：`SftpServerTool`（可在 services.msc 中查看，已配置失败自动重启）
- 服务模式下 SFTP 服务（默认 `0.0.0.0:2222`）与本地管理界面（`127.0.0.1:18080`）随服务自动启动
- 用户管理仍在管理界面中操作：浏览器打开 `http://127.0.0.1:18080`
- 停止服务：`net stop SftpServerTool`；卸载：`sc delete SftpServerTool`

## 将打包的 EXE 安装为 Windows 服务（免 Python 依赖）

已打包的 `dist\SftpServerTool.exe` 可通过 NSSM 注册为系统服务（`nssm.exe` 已随工具提供）：

```powershell
# 管理员 PowerShell，在工具目录执行：
.\nssm.exe install SftpServerToolSvc ".\dist\SftpServerTool.exe" "--no-browser" "--auto-start" "--port" "18080"
.\nssm.exe set SftpServerToolSvc AppDirectory ".\dist"
.\nssm.exe set SftpServerToolSvc Start SERVICE_AUTO_START
.\nssm.exe set SftpServerToolSvc AppExit Default Restart
net start SftpServerToolSvc
```

- EXE 服务模式下 SFTP（`0.0.0.0:2222`）与管理界面（`127.0.0.1:18080`）随服务自动启动
- 数据文件（config.json / users.json / host_key）生成于 `dist` 目录（exe 旁）
- 停止：`net stop SftpServerToolSvc`；卸载：`.\nssm.exe remove SftpServerToolSvc confirm`

> 提示：源码版服务（`SftpServerTool`，pywin32）与 EXE 服务（`SftpServerToolSvc`，NSSM）二选一，避免端口冲突；删除源码版服务用 `sc delete SftpServerTool`。

## 可选：打包为独立 EXE（免 Python 环境）

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name SftpServerTool --collect-all paramiko web_app.py
```

生成物在 `dist\SftpServerTool.exe`。注意：EXE 模式打开管理界面后，需保持程序运行
（托盘/窗口不可关闭），否则服务随进程退出。

## 配置示例

`users.json` 中的用户数据结构（密码为哈希，请通过界面添加，勿手写）：

```json
{
  "users": [
    {
      "username": "alice",
      "password_hash": "pbkdf2_sha256$210000$...$...",
      "root": "D:\\sftp\\alice",
      "permission": "readwrite",
      "enabled": true,
      "note": ""
    }
  ]
}
```

`permission` 取值：`readwrite`（读写）/ `readonly`（只读）。

