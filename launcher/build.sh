#!/bin/bash
# 构建 niu 启动器并复制到项目根目录
set -e
cd "$(dirname "$0")"

# 解析 --dmg 开关（生成 DMG 安装包），并从传给 cargo 的参数里过滤掉 --dmg
BUILD_DMG=false
CARGO_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dmg) BUILD_DMG=true ;;
    *) CARGO_ARGS+=("$arg") ;;
  esac
done
cargo build --release "${CARGO_ARGS[@]}"
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
    PROJECT_ROOT="$(cd .. && pwd)"

    # python/ (含自包含 stdlib + dylib + Resources stub)
    echo "[build.sh] copying python/ to bundle..."
    # 不用 -X：签名完全由 Step 2 codesign --force 重新打，避免 rsync -X 带入旧 xattr（含可能的 quarantine）
    # 排除 *.bak（relocate 脚本的备份文件不进 bundle）
    # 排除 igraph + leidenalg（GPL，用户按 README 用自包含 Python 手动安装）
    # 注意：igraph 在 PyPI 包名是 python-igraph，dist-info 目录用下划线 python_igraph-*
    # texttable 是 igraph 的依赖，也一并排除
    # cv2/insightface/easydict/pillow_heif：人脸识别 + HEIC，cv2 捆绑 GPL 版 FFmpeg（x264/x265），
    # pillow-heif 链接 libx265（GPLv2），用户需照片处理时按 README 自装
    rsync -a --delete --exclude='*.bak' \
        --exclude='igraph' --exclude='igraph-*.dist-info' --exclude='python_igraph-*.dist-info' \
        --exclude='leidenalg' --exclude='leidenalg-*.dist-info' \
        --exclude='texttable.py' --exclude='texttable-*.dist-info' \
        --exclude='cv2' --exclude='opencv_python_headless-*.dist-info' \
        --exclude='insightface' --exclude='insightface-*.dist-info' \
        --exclude='easydict' --exclude='easydict-*.dist-info' \
        --exclude='pillow_heif' --exclude='pillow_heif-*.dist-info' --exclude='_pillow_heif*.so' \
        "$PROJECT_ROOT/python/" "$RESOURCES_DIR/python/"

    # 清理 site-packages/bin/（pip install 产生的 console_scripts，shebang 指向开发机路径，
    # 运行时不使用——niu_api 用 python -m niu_api 走 sys.executable，不调 bin/ 脚本）
    # 通配 python3.* 避免版本号硬编码
    for bin_dir in "$RESOURCES_DIR"/python/lib/python3.*/site-packages/bin; do
        if [ -d "$bin_dir" ]; then
            rm -rf "$bin_dir"
            echo "[build.sh] removed $bin_dir (console_scripts with dev-machine shebang)"
        fi
    done

    # 对 bundle 内 python/ 跑 relocate（确保自包含）
    "$PROJECT_ROOT/scripts/relocate_python_framework.sh" "$RESOURCES_DIR/python"

    # ui/main/ (Electron)
    echo "[build.sh] copying ui/main/..."
    rsync -a --delete --exclude '.git' --exclude 'node_modules/.cache' \
        --exclude 'windows/assistant/fonts/AZhuPaoPaoTi.ttf' \
        "$PROJECT_ROOT/ui/main/" "$RESOURCES_DIR/ui/main/"

    # config/ (模板，运行时复制到 ~/.niu/config/)
    echo "[build.sh] copying config/..."
    rsync -a --delete "$PROJECT_ROOT/config/" "$RESOURCES_DIR/config/"

    # models/
    echo "[build.sh] copying models/..."
    # 排除 buffalo_l/*.onnx（InsightFace 非商业许可，用户首次用人脸识别时自动下载到 ~/.insightface/）
    rsync -a --delete --exclude='buffalo_l/*.onnx' \
        "$PROJECT_ROOT/models/" "$RESOURCES_DIR/models/"

    # memory/ (agent templates)
    echo "[build.sh] copying memory/..."
    rsync -a --delete "$PROJECT_ROOT/memory/" "$RESOURCES_DIR/memory/" 2>/dev/null || true

    # docs/ (系统手册 + 子文档 + 图片，只复制保留清单内的)
    echo "[build.sh] copying docs/..."
    mkdir -p "$RESOURCES_DIR/docs"
    for f in SYSTEM_MANUAL.md \
             manual-amap-setup.md manual-dependencies.md manual-developer.md \
             manual-feishu-setup.md manual-file-formats.md manual-general-subagent.md \
             manual-ha-setup.md manual-im-gateway.md manual-mcp-disk.md \
             manual-performance.md manual-troubleshooting.md manual-user-guide.md \
             manual-vector-store.md \
             kg-dev-dictionary.md; do
        if [ -f "$PROJECT_ROOT/docs/$f" ]; then
            cp "$PROJECT_ROOT/docs/$f" "$RESOURCES_DIR/docs/$f"
        fi
    done
    # 复制图片
    for f in "CHAT页面.png" "知识图谱.png"; do
        if [ -f "$PROJECT_ROOT/docs/$f" ]; then
            cp "$PROJECT_ROOT/docs/$f" "$RESOURCES_DIR/docs/$f"
        fi
    done

    # niu_api/ (Python API 模块，Python 启动时用 -m niu_api 找它)
    echo "[build.sh] copying niu_api/..."
    rsync -a --delete "$PROJECT_ROOT/niu_api/" "$RESOURCES_DIR/niu_api/"

    # agent/ (Agent 核心模块，niu_api 依赖)
    echo "[build.sh] copying agent/..."
    rsync -a --delete "$PROJECT_ROOT/agent/" "$RESOURCES_DIR/agent/"

    # mcp-servers/ (MCP 服务器 Python 模块，config/mcp-servers.yaml 用相对路径 workdir 引用)
    # --delete-excluded: 删除目标端已存在的 embedding-service（之前版本复制进去过）
    # embedding-service 已废弃（由 lightrag-server 统一替代），且含 bash.exe.stackdump 垃圾
    echo "[build.sh] copying mcp-servers/..."
    rsync -a --delete --delete-excluded \
        --exclude 'embedding-service' --exclude '__pycache__' --exclude '.DS_Store' \
        "$PROJECT_ROOT/mcp-servers/" "$RESOURCES_DIR/mcp-servers/"

    # im-adapters/ (IM Gateway 适配器，飞书等，gateway.py 用相对路径引用)
    # gateway.py L154: Path(__file__).resolve().parent.parent.parent / "im-adapters" / adapter_type / "src"
    # bundle 模式下 __file__ 是 niu.app/Contents/Resources/niu_api/channel/gateway.py，
    # parent.parent.parent 是 niu.app/Contents/Resources/，所以 bundle 内应有 im-adapters/feishu/src/
    echo "[build.sh] copying im-adapters/..."
    rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '.DS_Store' --exclude '*.pyc' \
        "$PROJECT_ROOT/im-adapters/" "$RESOURCES_DIR/im-adapters/"

    # extensions/niu-browser-ext/ (浏览器扩展，browser-server MCP 用 EXTENSION_DIR 引用)
    # mcp-servers/browser-server/src/niu_browser_server/launcher.py:16
    # EXTENSION_DIR = Path(__file__).parent.parent.parent.parent.parent / "extensions" / "niu-browser-ext"
    # parent^5 从 Resources/mcp-servers/browser-server/src/niu_browser_server/launcher.py
    # 回溯五级 = Resources/，拼 extensions/niu-browser-ext = Resources/extensions/niu-browser-ext/
    echo "[build.sh] copying extensions/niu-browser-ext/..."
    mkdir -p "$RESOURCES_DIR/extensions"
    rsync -a --delete --exclude '.git' --exclude 'node_modules' --exclude '.DS_Store' \
        "$PROJECT_ROOT/extensions/niu-browser-ext/" "$RESOURCES_DIR/extensions/niu-browser-ext/"

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

    # 主动加 com.apple.quarantine xattr：ad-hoc 签名 + 无 quarantine + Rust Mach-O
    # 会被 syspolicyd 拒绝启动（qtn_proc_init 失败）。带 quarantine 后首次双击弹
    # "无法验证开发者"对话框，用户点"打开"授权后系统记住，后续直接启动。
    QUARANTINE_ATTR="$(date +%s)|0x|||com.niu.launcher|$(uuidgen 2>/dev/null || echo '00000000-0000-0000-0000-000000000000')"
    xattr -w com.apple.quarantine "$QUARANTINE_ATTR" ../niu.app 2>/dev/null || true

    echo "[build.sh] macOS .app bundle created at ../niu.app (icon + ad-hoc signature + LaunchServices provenance + quarantine)"

    # 可选：生成 DMG 安装包（用 --dmg 开关启用）
    if [ "$BUILD_DMG" = "true" ]; then
        VERSION="$(cat "$PROJECT_ROOT/VERSION")"
        DIST_DIR="$PROJECT_ROOT/dist"
        DMG_NAME="Niu-${VERSION}-mac-intel.dmg"
        STAGE="/tmp/niu_dmg_stage_$$"
        echo "[build.sh] generating DMG: $DMG_NAME"
        rm -rf "$STAGE"
        mkdir -p "$STAGE"
        ln -sf /Applications "$STAGE/Applications"
        cp -R ../niu.app "$STAGE/niu.app"
        mkdir -p "$DIST_DIR"
        rm -f "$DIST_DIR/$DMG_NAME"
        hdiutil create -volname "Niu" -srcfolder "$STAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 "$DIST_DIR/$DMG_NAME"
        rm -rf "$STAGE"
        echo "[build.sh] DMG created at $DIST_DIR/$DMG_NAME"
    fi
fi

# 修复 node_modules/.bin/ 下的可执行权限（铁律 #7）
cd ..
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
cd launcher
