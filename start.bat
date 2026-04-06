@echo off
chcp 65001 >nul
REM Niu 个人知识助理 - Windows 启动脚本

echo ============================================================
echo Niu 个人知识助理启动中...
echo ============================================================

REM 检查是否首次启动
if not exist "%USERPROFILE%\.niu\initialized" (
    echo.
    echo 检测到首次启动，将在启动完成后初始化系统...
    echo.
    set FIRST_RUN=1
) else (
    set FIRST_RUN=0
)

REM 启动主程序
start "" "%~dp0niu-assistant.exe"

REM 如果是首次启动，等待并注入系统说明书
if "%FIRST_RUN%"=="1" (
    echo.
    echo 等待程序启动完成...
    timeout /t 30 /nobreak >nul

    echo.
    echo 注入系统说明书到向量库...
    python "%~dp0scripts\inject_system_manual.py"

    if %ERRORLEVEL%==0 (
        echo.
        echo ✅ 初始化完成
        echo "%USERPROFILE%\.niu\initialized" >nul 2>&1
    ) else (
        echo.
        echo ⚠️  系统说明书注入失败，请手动执行：
        echo     python scripts\inject_system_manual.py
    )
)

echo.
echo 程序已在后台启动，窗口可以关闭
pause
