@echo off
REM ============================================
REM Niu Windows 打包脚本
REM 用法: pack.bat
REM 前置: 已安装 7-Zip (C:\Program Files\7-Zip\7z.exe)
REM 产物: dist\Niu-<VERSION>-win-x64.7z
REM ============================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

REM 检查 7-Zip
set SEVENZIP=E:\Program Files\7-Zip\7z.exe
if not exist "!SEVENZIP!" (
    echo [pack.bat] ERROR: 7-Zip not found at !SEVENZIP!
    echo [pack.bat] Please install 7-Zip from https://7-zip.org/
    exit /b 1
)

REM 读取 VERSION
set /p VERSION=<VERSION
set DIST_DIR=dist
set ARCHIVE_NAME=Niu-!VERSION!-win-x64.7z
set STAGE=temp_pack_stage

echo [pack.bat] Packaging Niu !VERSION! for Windows x64

REM === 清理不需要的文件（不进 7z，也不需要保留）===
echo [pack.bat] Skipping launcher/target/ (excluded from 7z via robocopy /xd)
echo [pack.bat] Cleaning __pycache__...
for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d" 2>nul
del /s /q "*.pyc" 2>nul

REM 清理旧的产物和临时目录
if exist "!DIST_DIR!\!ARCHIVE_NAME!" del "!DIST_DIR!\!ARCHIVE_NAME!"
if exist "!STAGE!" rmdir /s /q "!STAGE!"
mkdir "!STAGE!"
mkdir "!DIST_DIR!"

REM === 复制需要打包的文件到临时目录 ===
REM 排除: 编译产物、.git、缓存、备份、开发工具配置
echo [pack.bat] Copying files...
robocopy . "!STAGE!" /E ^
    /xd launcher\target .git backup temp_pack_stage dist .pytest_cache .ruff_cache .gitnexus .sisyphus .playwright-mcp .claude ^
    /xf *.pyc niu.exe~ *.bak .DS_Store

REM 确保 niu.exe 在根目录
if not exist "!STAGE!\niu.exe" (
    echo [pack.bat] ERROR: niu.exe not found. Run launcher/build.sh first.
    exit /b 1
)

REM 删除临时目录里残留的 __pycache__
for /d /r "!STAGE!" %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d" 2>nul
del /s /q "!STAGE!\*.pyc" 2>nul

REM 用 7-Zip 压缩（LZMA2，压缩率高）
echo [pack.bat] Creating 7z archive...
"!SEVENZIP!" a -t7z -mx=9 -mmt=on "!DIST_DIR!\!ARCHIVE_NAME!" "!STAGE!\*"

REM 清理临时目录
rmdir /s /q "!STAGE!"

echo [pack.bat] Done: !DIST_DIR!\!ARCHIVE_NAME!
endlocal
pause
