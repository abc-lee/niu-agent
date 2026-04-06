#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载 HuggingFace 模型到本地

用法：python scripts/download_model.py
"""

import os
import sys
from pathlib import Path

def download_model():
    """下载多语言embedding模型"""

    print("=" * 60)
    print("Download HuggingFace Model")
    print("=" * 60)

    # 模型信息
    model_id = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # model_id = "DataikuNLP/paraphrase-multilingual-MiniLM-L12-v2"  # 备选

    # 目标目录
    project_root = Path(__file__).parent.parent
    models_dir = project_root / "models"
    target_dir = models_dir / "paraphrase-multilingual-MiniLM-L12-v2"

    print(f"\nModel: {model_id}")
    print(f"Target: {target_dir}")
    print()

    # 检查是否已存在
    if target_dir.exists():
        print(f"Model already exists at: {target_dir}")
        response = input("Overwrite? (y/N): ").strip().lower()
        if response != 'y':
            print("Cancelled.")
            return
        print("Removing old files...")
        import shutil
        shutil.rmtree(target_dir)

    # 创建目录
    target_dir.mkdir(parents=True, exist_ok=True)

    # 安装依赖
    try:
        from huggingface_hub import snapshot_download
        print("[OK] huggingface_hub installed")
    except ImportError:
        print("Installing huggingface_hub...")
        os.system(f"{sys.executable} -m pip install huggingface_hub")
        from huggingface_hub import snapshot_download

    # 下载模型
    print("\nDownloading model files...")
    print("This may take several minutes (model size: ~420MB)")
    print()

    try:
        snapshot_download(
            repo_id=model_id,
            local_dir=str(target_dir),
            local_dir_use_symlinks=False,  # 不使用符号链接，直接下载文件
            resume_download=True,  # 支持断点续传
            ignore_patterns=[
                "*.git*",  # 不下载Git相关文件
                "*.h5",    # 不下载TensorFlow格式
                "onnx/*",  # 不下载ONNX格式
                "openvino/*",  # 不下载OpenVINO格式
            ]
        )

        print("\n" + "=" * 60)
        print("Download completed!")
        print("=" * 60)
        print(f"\nModel saved to: {target_dir}")
        print("\nFiles downloaded:")
        for file in sorted(target_dir.iterdir()):
            size_mb = file.stat().st_size / (1024 * 1024)
            print(f"  - {file.name} ({size_mb:.1f} MB)")

        print("\nNext steps:")
        print("1. Update embedding service to use new model")
        print("2. Restart API server")

    except Exception as e:
        print(f"\n[ERROR] Download failed: {e}")
        print("\nPossible solutions:")
        print("1. Check internet connection")
        print("2. Try alternative model:")
        print("   sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        print("3. Download manually from:")
        print(f"   https://huggingface.co/{model_id}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(download_model() or 0)
