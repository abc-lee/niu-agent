@echo off
echo ========================================
echo 清理测试进程脚本
echo ========================================
echo.

echo 当前的 Python 进程：
tasklist | findstr python.exe
echo.

echo ========================================
echo 请手动确认哪些进程需要关闭：
echo.
echo 通常需要关闭的测试进程特征：
echo   - 占用内存较大 (^>50MB)
echo   - 包含 "test_skills" 或 "injector" 关键词
echo.
echo LSP 进程特征：
echo   - 占用内存较小 (^<10MB)
echo   - 命令行包含 pyls, pyright, jedi 等
echo ========================================
echo.

set /p CONFIRM="是否显示详细进程信息？(y/n): "
if /i "%CONFIRM%"=="y" (
    wmic process where "name='python.exe'" get processid,commandline 2>nul
)

echo.
set /p PIDLIST="请输入要关闭的进程ID（用空格分隔，回车跳过）: "

if not "%PIDLIST%"=="" (
    for %%p in (%PIDLIST%) do (
        echo 关闭进程 %%p...
        taskkill /F /PID %%p 2>nul
    )
    echo.
    echo 清理完成！当前进程：
    tasklist | findstr python.exe
) else (
    echo 跳过清理。
)

pause
