@echo off
REM ============================================
REM Niu Windows 打包脚本
REM 用法: pack.bat
REM 前置: 已安装 7-Zip (C:\Program Files\7-Zip\7z.exe)
REM 产物: dist\Niu-<VERSION>-win-x64.7z
REM ============================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

REM 检查 7-Zip（官方默认安装路径优先，回退作者机器路径）
if exist "C:\Program Files\7-Zip\7z.exe" (
    set SEVENZIP=C:\Program Files\7-Zip\7z.exe
) else if exist "E:\Program Files\7-Zip\7z.exe" (
    set SEVENZIP=E:\Program Files\7-Zip\7z.exe
) else (
    echo [pack.bat] ERROR: 7-Zip not found in C:\ or E:\Program Files\7-Zip\
    echo [pack.bat] Install from https://7-zip.org/ or add your path as another branch above
    exit /b 1
)

REM 检查 ui\main\node_modules（Electron 依赖；缺失时 GUI 起不来，属静默残包）
if not exist "ui\main\node_modules\electron" (
    echo [pack.bat] ERROR: ui\main\node_modules not installed. Run: cd ui\main ^&^& npm install
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

REM === 构建 niu-natives（Rust PyO3 原生扩展 → wheel 装进 python\）===
REM 必须在使用 robocopy 复制 python\ 之前完成——screenshot / list_targets 依赖该 .pyd，
REM 缺失时 vision-server 会静默降级为错误串（不崩但功能不可用）。
REM 与 launcher/build.sh 的 maturin 步骤同构；.pyd 不进 git，clone 后必须本机构建。
if not exist "python\Scripts\maturin.exe" (
    echo [pack.bat] ERROR: python\Scripts\maturin.exe not found
    echo [pack.bat] Run: python\Scripts\pip.exe install -r requirements-dev.txt
    exit /b 1
)
echo [pack.bat] Building niu-natives wheel...
if exist "niu-natives\target\wheels" del /q "niu-natives\target\wheels\*.whl"
pushd niu-natives
"..\python\Scripts\maturin.exe" build --release -i "..\python\Scripts\python.exe"
if errorlevel 1 (
    echo [pack.bat] ERROR: maturin build failed
    popd
    exit /b 1
)
popd
for %%f in ("niu-natives\target\wheels\niu_natives-*.whl") do (
    "python\Scripts\pip.exe" install --force-reinstall "%%f"
)
if errorlevel 1 (
    echo [pack.bat] ERROR: pip install niu-natives wheel failed
    exit /b 1
)
REM 守卫：确认 .pyd 真落进 python\，否则打出的包缺桌面截图能力
dir /b "python\Lib\site-packages\niu_natives\*.pyd" >nul 2>&1
if errorlevel 1 (
    echo [pack.bat] ERROR: niu_natives .pyd missing after install
    exit /b 1
)
echo [pack.bat] niu-natives installed into python\

REM === 复制需要打包的文件到临时目录 ===
REM 排除: 编译产物、.git、缓存、备份、开发工具配置
echo [pack.bat] Copying files...
robocopy . "!STAGE!" /E ^
    /xd .git backup temp_pack_stage dist .pytest_cache .ruff_cache .sisyphus .playwright-mcp .claude target niu-natives ^
        docs\lightrag-plans docs\superpowers ^
    /xf *.pyc niu.exe~ *.bak .DS_Store docs\kg-dev-dictionary.md

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
