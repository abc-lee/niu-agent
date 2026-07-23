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

    # ad-hoc 签名（必须在清 xattr 之前完成，否则签名结果被 xattr 影响）
    codesign --force --deep --sign - ../niu.app 2>/dev/null || echo "[build.sh] WARNING: codesign failed (non-fatal)"

    # 注册到 LaunchServices（刷新 Finder 缓存，让新 icon + bundle 生效）
    # 注意：lsregister 会给 bundle 加 com.apple.provenance xattr，所以必须在清 xattr 之前执行
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f ../niu.app 2>/dev/null || true

    # 清除所有 xattr（含 com.apple.provenance），然后主动加 com.apple.quarantine
    # 这是 macOS Sequoia 上 ad-hoc 签名的 .app 双击启动的标准方案：
    #   - 无 quarantine + ad-hoc 签名 → spctl reject → 双击"一闪退出"无提示
    #   - 有 quarantine → 首次双击弹"无法验证开发者"对话框，用户点"打开"后系统记住授权，
    #     后续双击直接启动不弹对话框（用户接受首次授权，不能每次都弹）
    # 必须放在所有 LaunchServices 操作之后，作为最后一步
    xattr -cr ../niu.app 2>/dev/null || true

    # 主动加 com.apple.quarantine xattr
    # 格式：Quarantine 时间戳 | agent | bundle id | UUID
    QUARANTINE_ATTR="$(date +%s)|0x|||com.niu.launcher|$(uuidgen 2>/dev/null || echo '00000000-0000-0000-0000-000000000000')"
    xattr -w com.apple.quarantine "$QUARANTINE_ATTR" ../niu.app 2>/dev/null || true

    echo "[build.sh] macOS .app bundle created at ../niu.app (icon + quarantine for first-open authorization)"
fi

# 修复 node_modules/.bin/ 下的可执行权限（铁律 #7）
cd ..
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
cd launcher
