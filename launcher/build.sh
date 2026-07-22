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
    mkdir -p "$APP_DIR"
    cp target/release/niu-launcher "$APP_DIR/niu"
    # 写最小 Info.plist（LSUIElement=true 隐藏 Dock + 不弹 Terminal）
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
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
</dict>
</plist>
PLIST
    # PkgInfo 文件（8 字节，APPL=application）
    echo -n "APPL????" > "../niu.app/Contents/PkgInfo"
    # ad-hoc 签名（未签名 bundle 在某些 macOS 配置下 open 会失败）
    codesign --force --deep --sign - ../niu.app 2>/dev/null || true
    echo "[build.sh] macOS .app bundle created at ../niu.app"
fi

# 修复 node_modules/.bin/ 下的可执行权限（铁律 #7）
cd ..
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
cd launcher
