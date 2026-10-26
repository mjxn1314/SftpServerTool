@echo off
cd /d "%~dp0"
python -c "import paramiko" >nul 2>&1
if errorlevel 1 (
    echo [setup] paramiko not found, installing...
    python -m pip install paramiko
    if errorlevel 1 (
        echo [error] Failed to install paramiko.
        echo [error] Please install Python 3.8+ from python.org and run again.
        pause
        exit /b 1
    )
)
echo Starting SFTP server management tool...
python web_app.py
if errorlevel 1 pause
