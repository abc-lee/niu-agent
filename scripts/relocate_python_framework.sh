#!/bin/bash
# 把系统 Python.framework 的 stdlib + dylib + Resources stub 复制进 python/，
# 改 python3 + dylib install_name 指向 @rpath/libPython3.11.dylib，
# 重签 python3 + dylib。
#
# 用法：./scripts/relocate_python_framework.sh [python_dir]
#   python_dir 默认 ./python。脚本对该目录做就地改造。
#   build.sh 调用时传入 bundle 内 python/ 目录路径。

set -e

PYTHON_DIR="${1:-./python}"
FRAMEWORK_PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/Python"
FRAMEWORK_LIB="/Library/Frameworks/Python.framework/Versions/3.11/lib/python3.11"
FRAMEWORK_RESOURCES="/Library/Frameworks/Python.framework/Versions/3.11/Resources"

if [ ! -f "$FRAMEWORK_PYTHON" ]; then
    echo "[relocate] ERROR: $FRAMEWORK_PYTHON not found"
    exit 1
fi

if [ ! -d "$PYTHON_DIR" ]; then
    echo "[relocate] ERROR: $PYTHON_DIR not found"
    exit 1
fi

# Step 0: 备份 + otool 留证
echo "[relocate] Step 0: backup + otool 留证"
cp "$PYTHON_DIR/bin/python3" "$PYTHON_DIR/bin/python3.bak"
otool -L "$PYTHON_DIR/bin/python3" > /tmp/python3_otool_before.txt 2>&1

# Step 1: 复制 stdlib 到 python/lib/python3.11/（排除 site-packages 已有）
echo "[relocate] Step 1: copy stdlib"
rsync -a --exclude='site-packages' "$FRAMEWORK_LIB/" "$PYTHON_DIR/lib/python3.11/"

# Step 2: 复制 Python dylib
echo "[relocate] Step 2: copy Python dylib"
cp "$FRAMEWORK_PYTHON" "$PYTHON_DIR/lib/libPython3.11.dylib"

# Step 3: 复制 Resources/Python.app stub（framework 模式 python3 启动需要）
echo "[relocate] Step 3: copy Resources stub"
cp -R "$FRAMEWORK_RESOURCES" "$PYTHON_DIR/lib/Resources"

# Step 4: 改 dylib install_name (id) 为 @rpath/libPython3.11.dylib
echo "[relocate] Step 4: set dylib install_name"
install_name_tool -id @rpath/libPython3.11.dylib "$PYTHON_DIR/lib/libPython3.11.dylib"

# Step 5: 改 python3 二进制的 dylib 引用
echo "[relocate] Step 5: change python3 binary dylib reference"
install_name_tool -change \
    "$FRAMEWORK_PYTHON" \
    @rpath/libPython3.11.dylib \
    "$PYTHON_DIR/bin/python3"

# Step 6: 加 @loader_path/../lib rpath
echo "[relocate] Step 6: add rpath"
install_name_tool -add_rpath @loader_path/../lib "$PYTHON_DIR/bin/python3" 2>/dev/null || \
    echo "[relocate] (rpath already exists, skipping)"

# Step 7: 重签 python3 + dylib
echo "[relocate] Step 7: re-sign"
codesign --force --sign - "$PYTHON_DIR/bin/python3"
codesign --force --sign - "$PYTHON_DIR/lib/libPython3.11.dylib"

# Step 8: 验证（用 PYTHONHOME 模拟启动器行为）
echo "[relocate] Step 8: verify with PYTHONHOME"
PYTHONHOME_ABS=$(cd "$PYTHON_DIR" && pwd)
echo "--- otool -L python3 (should show @rpath/libPython3.11.dylib) ---"
otool -L "$PYTHON_DIR/bin/python3" | grep -i python
echo "--- import numpy/torch ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import numpy; print('numpy', numpy.__version__)"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import torch; print('torch', torch.__version__)"
echo "--- encodings __file__ (should be bundle-internal) ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import encodings; print(encodings.__file__)"
echo "--- sys.prefix / base_prefix ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import sys; print('prefix:', sys.prefix); print('base_prefix:', sys.base_prefix)"

echo "[relocate] DONE"
