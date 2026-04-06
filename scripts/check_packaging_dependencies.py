#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包前依赖检查脚本

验证所有依赖是否正确声明，确保打包完整性。
"""

import subprocess
import sys
import io
from pathlib import Path

# 设置标准输出编码为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def check_dependencies():
    """检查依赖完整性"""
    print("=" * 60)
    print("依赖完整性检查")
    print("=" * 60)

    # 1. 检查 pyproject.toml 声明
    print("\n1. 检查 pyproject.toml 声明...")

    pyproject_files = [
        "niu_api/pyproject.toml",
        "mcp-servers/photo-server/pyproject.toml",
    ]

    for pyproject in pyproject_files:
        path = Path(__file__).parent.parent / pyproject
        if not path.exists():
            print(f"  ❌ {pyproject} 不存在")
            continue

        print(f"\n  {pyproject}:")

        # 解析 dependencies
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)

        deps = data.get("project", {}).get("dependencies", [])
        print(f"    依赖数量: {len(deps)}")
        for dep in deps:
            print(f"      - {dep}")

        # 检查可选依赖
        optional = data.get("project", {}).get("optional-dependencies", {})
        if optional:
            print(f"    可选依赖:")
            for group, group_deps in optional.items():
                print(f"      [{group}]: {len(group_deps)} 个")

    # 2. 检查实际安装的包
    print("\n2. 检查实际安装的包...")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=freeze"],
        capture_output=True,
        text=True,
    )

    installed = {}
    for line in result.stdout.split("\n"):
        if "==" in line:
            name, version = line.split("==")
            installed[name.lower()] = version

    # 3. 检查关键依赖
    print("\n3. 检查关键依赖...")

    critical_deps = {
        "onnxruntime": "ONNX Runtime（CPU/GPU）",
        "insightface": "人脸识别库",
        "opencv-python-headless": "OpenCV（无 GUI）",
        "sentence-transformers": "向量模型库",
    }

    for dep_name, desc in critical_deps.items():
        if dep_name in installed:
            print(f"  ✅ {desc}: {installed[dep_name]}")
        else:
            print(f"  ❌ {desc}: 未安装")

    # 4. 检查 ONNX Runtime 版本
    print("\n4. 检查 ONNX Runtime 版本...")

    onnx_versions = []
    for name in installed:
        if "onnxruntime" in name:
            onnx_versions.append(f"{name}=={installed[name]}")

    if len(onnx_versions) > 1:
        print("  ⚠️  检测到多个 ONNX Runtime 版本共存：")
        for ver in onnx_versions:
            print(f"    - {ver}")
        print("\n  建议：只保留一个版本（onnxruntime 或 onnxruntime-gpu）")
    elif len(onnx_versions) == 1:
        print(f"  ✅ {onnx_versions[0]}")
    else:
        print("  ❌ 未安装 ONNX Runtime")

    # 5. 检查 GPU 支持
    print("\n5. 检查 GPU 支持...")

    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        print(f"  可用的 ExecutionProvider: {providers}")

        if "CUDAExecutionProvider" in providers:
            print("  ✅ 支持 CUDA GPU 加速")
        else:
            print("  ⚠️  不支持 CUDA GPU 加速")

        if "DmlExecutionProvider" in providers:
            print("  ✅ 支持 DirectML GPU 加速（Windows）")
    except ImportError:
        print("  ❌ onnxruntime 未安装")

    print("\n" + "=" * 60)
    print("检查完成")
    print("=" * 60)


if __name__ == "__main__":
    check_dependencies()
