#!/bin/bash
# 构建 niu 启动器并复制到项目根目录
set -e
cd "$(dirname "$0")"
cargo build --release "$@"
cp target/release/niu-launcher ../niu
echo "Built and copied to ../niu"

# macOS: 构造 .app bundle（让 Finder 双击不弹 Terminal）
if [ "$(uname)" = "Darwin" ]; then
    APP_DIR="../niu.app/Contents/MacOS"
    RESOURCES_DIR="../niu.app/Contents/Resources"
    ICONSET_DIR="../niu.iconset"
    ICONS_SRC="../ui/main/windows/assistant/icons"
    mkdir -p "$APP_DIR" "$RESOURCES_DIR"
    cp target/release/niu-launcher "$APP_DIR/niu"

    # --- 复制运行时资源到 Contents/Resources/ ---
    RESOURCES_DIR_FULL="$RESOURCES_DIR"
    PROJECT_ROOT_FULL="$(cd .. && pwd)"

    # python/ (含自包含 stdlib + dylib + Resources stub)
    echo "[build.sh] copying python/ to bundle..."
    # 不用 -X：签名完全由 Step 2 codesign --force 重新打，避免 rsync -X 带入旧 xattr（含可能的 quarantine）
    # 排除 *.bak（relocate 脚本的备份文件不进 bundle）
    rsync -a --delete --exclude='*.bak' "$PROJECT_ROOT_FULL/python/" "$RESOURCES_DIR_FULL/python/"
    # 对 bundle 内 python/ 跑 relocate（确保自包含）
    "$PROJECT_ROOT_FULL/scripts/relocate_python_framework.sh" "$RESOURCES_DIR_FULL/python"

    # ui/main/ (Electron)
    echo "[build.sh] copying ui/main/..."
    rsync -a --delete --exclude '.git' --exclude 'node_modules/.cache' \
        "$PROJECT_ROOT_FULL/ui/main/" "$RESOURCES_DIR_FULL/ui/main/"

    # config/ (模板，运行时复制到 ~/.niu/config/)
    echo "[build.sh] copying config/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/config/" "$RESOURCES_DIR_FULL/config/"

    # models/
    echo "[build.sh] copying models/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/models/" "$RESOURCES_DIR_FULL/models/"

    # memory/ (agent templates)
    echo "[build.sh] copying memory/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/memory/" "$RESOURCES_DIR_FULL/memory/" 2>/dev/null || true

    # niu_api/ (Python API 模块，Python 启动时用 -m niu_api 找它)
    echo "[build.sh] copying niu_api/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/niu_api/" "$RESOURCES_DIR_FULL/niu_api/"

    # agent/ (Agent 核心模块，niu_api 依赖)
    echo "[build.sh] copying agent/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/agent/" "$RESOURCES_DIR_FULL/agent/"

    # mcp-servers/ (MCP 服务器 Python 模块，config/mcp-servers.yaml 用相对路径 workdir 引用)
    echo "[build.sh] copying mcp-servers/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/mcp-servers/" "$RESOURCES_DIR_FULL/mcp-servers/"

    # 构造 iconset（从 ui/main/windows/assistant/icons 复制 PNG 改名 + sips 强制正方形）
    # 源 PNG 是非正方形（16x18/32x37/64x75/128x151 等），iconutil 严格校验像素必须匹配命名尺寸，
    # 否则生成失败。用 sips --resampleHeightWidth 强制到正方形像素再放 iconset。
    if [ -d "$ICONS_SRC" ]; then
        mkdir -p "$ICONSET_DIR"
        # 格式: src_png  target_size
        make_icon() {
            local src="$1"; local size="$2"; local out="$3"
            if [ -f "$src" ]; then
                # sips -z height width（resample 到正方形，PNG 不支持 -s pixelWidth）
                sips -z "$size" "$size" "$src" --out "$out" >/dev/null 2>&1 || cp "$src" "$out"
            fi
        }
        make_icon "$ICONS_SRC/icon-16.png"  16  "$ICONSET_DIR/icon_16x16.png"
        make_icon "$ICONS_SRC/icon-32.png"  32  "$ICONSET_DIR/icon_16x16@2x.png"
        make_icon "$ICONS_SRC/icon-32.png"  32  "$ICONSET_DIR/icon_32x32.png"
        make_icon "$ICONS_SRC/icon-64.png"  64  "$ICONSET_DIR/icon_32x32@2x.png"
        make_icon "$ICONS_SRC/icon-128.png" 128 "$ICONSET_DIR/icon_128x128.png"
        make_icon "$ICONS_SRC/icon-256.png" 256 "$ICONSET_DIR/icon_128x128@2x.png"
        make_icon "$ICONS_SRC/icon-256.png" 256 "$ICONSET_DIR/icon_256x256.png"
        # 生成 icns（非致命：iconutil 失败不影响 bundle 启动）
        if iconutil -c icns "$ICONSET_DIR" -o "$RESOURCES_DIR/niu.icns" 2>/dev/null; then
            echo "[build.sh] icns generated: $RESOURCES_DIR/niu.icns"
        else
            echo "[build.sh] WARNING: iconutil failed (non-fatal, bundle will have no icon)"
        fi
        rm -rf "$ICONSET_DIR"
    else
        echo "[build.sh] WARNING: icons source dir not found: $ICONS_SRC"
    fi

    # 写 Info.plist（含 CFBundleIconFile 指向 niu.icns）
    cat > "../niu.app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.niu.launcher</string>
    <key>CFBundleExecutable</key>
    <string>niu</string>
    <key>CFBundleName</key>
    <string>Niu</string>
    <key>CFBundleIconFile</key>
    <string>niu</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
</dict>
</plist>
PLIST
    # PkgInfo 文件（8 字节，APPL=application）
    echo -n "APPL????" > "../niu.app/Contents/PkgInfo"

    # ad-hoc 签名（必须先签，再 lsregister）
    # 逐个签名 .so / .dylib / 二进制（codesign --deep 自 macOS 13.3 起废弃）
    # 顺序：inside-out（先签依赖的 dylib/.so，再签 python3，最后签顶层 bundle）
    # 并行：xargs -n 1 -P 4（-I{} 会禁用 -P，用 -n 1 替代）
    echo "[build.sh] signing Python .so + .dylib (parallel)..."
    find ../niu.app/Contents/Resources/python -type f \( -name "*.so" -o -name "*.dylib" \) -not -name "*.bak" -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true

    echo "[build.sh] signing python3 binary..."
    codesign --force --sign - ../niu.app/Contents/Resources/python/bin/python3 2>/dev/null || true

    echo "[build.sh] signing Electron main + Helper + Framework..."
    # Electron 二进制实际路径（实测）：
    # - Electron.app/Contents/MacOS/Electron (主二进制)
    # - Electron.app/Contents/Frameworks/Electron Helper.app/Contents/MacOS/Electron Helper
    # - Electron.app/Contents/Frameworks/Electron Helper (GPU/Renderer/Plugin).app/Contents/MacOS/Electron Helper (GPU/Renderer/Plugin)
    # - Electron.app/Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework
    # - Electron.app/Contents/Frameworks/Electron Framework.framework/Electron Framework (symlink，跳过)
    find ../niu.app/Contents/Resources/ui/main -type f \( -name "Electron" -o -name "Electron Helper*" -o -name "Electron Framework" -o -name "*.node" \) -not -name "*.bak" -not -type l -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true

    # 最后签 bundle 顶层（不 --deep）
    echo "[build.sh] signing top-level bundle..."
    codesign --force --sign - ../niu.app 2>/dev/null || echo "[build.sh] WARNING: codesign top-level failed (non-fatal)"

    # 注册到 LaunchServices（让 Finder 识别 icon + bundle + 打 com.apple.provenance xattr）
    # 注意：provenance xattr 是 macOS Sequoia Gatekeeper 启动 .app 的必需项，禁止清掉！
    # 之前 `xattr -cr` 清掉 provenance 后，启动时报 "ASP: Unable to apply provenance sandbox"
    # 被 launchd 立即 termination reported（症状：Finder 双击一闪退出）
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f ../niu.app 2>/dev/null || true

    # 主动加 com.apple.quarantine xattr
    # 根因：syspolicyd 的 qtn_proc 跟踪机制需要 quarantine xattr 才能正确初始化（qtn_proc_init），
    # 否则报 "Unable to initialize qtn_proc: 3" → "dispatch_mig_server returned 268435459"
    # → launchd 立即 termination reported。
    # ad-hoc 签名 + 无 quarantine + Rust Mach-O 二进制 → syspolicyd 拒绝启动
    # （bash 脚本 .app 不走 qtn_proc 路径所以能启动，Rust 二进制走该路径被拒）。
    # 带 quarantine 后首次双击弹"无法验证开发者"对话框，用户点"打开"授权后系统记住，后续直接启动。
    # 格式：Quarantine 时间戳 | agent | bundle id | UUID
    QUARANTINE_ATTR="$(date +%s)|0x|||com.niu.launcher|$(uuidgen 2>/dev/null || echo '00000000-0000-0000-0000-000000000000')"
    xattr -w com.apple.quarantine "$QUARANTINE_ATTR" ../niu.app 2>/dev/null || true

    echo "[build.sh] macOS .app bundle created at ../niu.app (icon + ad-hoc signature + LaunchServices provenance + quarantine)"
fi

# 修复 node_modules/.bin/ 下的可执行权限（铁律 #7）
cd ..
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
cd launcher
