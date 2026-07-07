#!/bin/bash
# 构建 niu 启动器并复制到项目根目录
set -e
cd "$(dirname "$0")"
cargo build --release "$@"
cp target/release/niu-launcher ../niu
echo "Built and copied to ../niu"

# 修复 node_modules/.bin/ 下的可执行权限（铁律 #7）
cd ..
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
cd launcher
