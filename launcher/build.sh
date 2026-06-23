#!/bin/bash
# 构建 niu 启动器并复制到项目根目录
set -e
cd "$(dirname "$0")"
cargo build --release "$@"
cp target/release/niu-launcher ../niu
echo "Built and copied to ../niu"
