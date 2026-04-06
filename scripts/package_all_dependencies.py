#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整依赖打包脚本

用途：下载所有依赖（Python 包 + 模型文件）到本地目录
确保打包时所有内容都在项目中，无需运行时下载
"""

import os
import sys
import subprocess
from pathlib import Path
import urllib.request
import zipfile
from tqdm import tqdm


class DependencyPackager:
    """依赖打包器"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.models_dir = project_root / "models"
        self.python_packages_dir = project_root / "python_packages"

        # 创建目录
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.python_packages_dir.mkdir(parents=True, exist_ok=True)

    def download_face_model(self):
        """下载人脸识别模型（buffalo_l）"""
        print("\n" + "=" * 60)
        print("下载人脸识别模型 (buffalo_l, 326MB)")
        print("=" * 60)

        model_dir = self.models_dir / "buffalo_l"
        model_dir.mkdir(parents=True, exist_ok=True)

        # 检查是否已存在
        required_files = [
            "det_10g.onnx",      # 人脸检测
            "w600k_r50.onnx",    # 人脸识别
            "2d106det.onnx",     # 2D 关键点
            "genderage.onnx",    # 性别年龄
        ]

        all_exist = all((model_dir / f).exists() for f in required_files)
        if all_exist:
            print("✅ 人脸识别模型已存在，跳过下载")
            return

        # 下载 URL
        url = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
        zip_path = self.models_dir / "buffalo_l.zip"

        print(f"下载地址: {url}")
        print(f"保存到: {zip_path}")

        # 下载
        try:
            with tqdm(unit='B', unit_scale=True, desc="下载中") as pbar:
                urllib.request.urlretrieve(
                    url,
                    zip_path,
                    reporthook=lambda b, bsize, t: pbar.update(bsize)
                )

            print("\n解压中...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.models_dir)

            # 清理
            zip_path.unlink()

            print(f"✅ 人脸识别模型已下载到: {model_dir}")

        except Exception as e:
            print(f"❌ 下载失败: {e}")
            print("\n请手动下载：")
            print(f"1. 访问: {url}")
            print(f"2. 下载后解压到: {model_dir}")

    def download_embedding_model(self):
        """下载向量模型"""
        print("\n" + "=" * 60)
        print("下载向量模型 (paraphrase-multilingual-MiniLM-L12-v2, 466MB)")
        print("=" * 60)

        model_dir = self.models_dir / "paraphrase-multilingual-MiniLM-L12-v2"

        if model_dir.exists():
            print(f"✅ 向量模型已存在: {model_dir}")
            return

        print("使用 sentence-transformers 下载...")

        try:
            from sentence_transformers import SentenceTransformer

            # 下载并保存模型
            model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
            model.save(str(model_dir))

            print(f"✅ 向量模型已下载到: {model_dir}")

        except Exception as e:
            print(f"❌ 下载失败: {e}")
            print("\n首次启动时会自动下载，或手动运行：")
            print("  python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2').save('models/paraphrase-multilingual-MiniLM-L12-v2')\"")

    def install_python_packages(self):
        """下载所有 Python 包到本地目录"""
        print("\n" + "=" * 60)
        print("下载 Python 依赖包")
        print("=" * 60)

        requirements_file = self.project_root / "requirements.txt"

        if not requirements_file.exists():
            print(f"❌ requirements.txt 不存在: {requirements_file}")
            return

        print(f"使用 pip download 下载所有依赖到: {self.python_packages_dir}")

        # 使用 pip download 下载所有依赖（包括平台特定包）
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "-r", str(requirements_file),
            "-d", str(self.python_packages_dir),
            "--platform", "win_amd64",  # Windows 64 位
            "--python-version", "3.11",
            "--only-binary=:all:",
        ]

        print(f"执行: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

            if result.returncode == 0:
                print(f"✅ Python 包已下载到: {self.python_packages_dir}")

                # 统计文件
                files = list(self.python_packages_dir.glob("*.whl"))
                print(f"   共 {len(files)} 个 wheel 文件")
            else:
                print(f"❌ 下载失败:")
                print(result.stderr)

        except subprocess.TimeoutExpired:
            print("❌ 下载超时（10分钟）")
        except Exception as e:
            print(f"❌ 下载失败: {e}")

    def verify(self):
        """验证依赖完整性"""
        print("\n" + "=" * 60)
        print("验证依赖完整性")
        print("=" * 60)

        # 1. 检查人脸识别模型
        face_model_dir = self.models_dir / "buffalo_l"
        required_files = ["det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx", "genderage.onnx"]

        face_ok = all((face_model_dir / f).exists() for f in required_files)
        print(f"{'✅' if face_ok else '❌'} 人脸识别模型: {face_model_dir}")

        # 2. 检查向量模型
        embedding_model_dir = self.models_dir / "paraphrase-multilingual-MiniLM-L12-v2"
        embedding_ok = embedding_model_dir.exists()
        print(f"{'✅' if embedding_ok else '❌'} 向量模型: {embedding_model_dir}")

        # 3. 检查 Python 包
        wheel_files = list(self.python_packages_dir.glob("*.whl"))
        python_ok = len(wheel_files) > 0
        print(f"{'✅' if python_ok else '❌'} Python 包: {len(wheel_files)} 个 wheel 文件")

        # 4. 检查系统说明书
        manual_path = self.project_root / "docs" / "SYSTEM_MANUAL.md"
        manual_ok = manual_path.exists()
        print(f"{'✅' if manual_ok else '❌'} 系统说明书: {manual_path}")

        # 总结
        all_ok = face_ok and embedding_ok and python_ok and manual_ok
        print("\n" + "=" * 60)
        if all_ok:
            print("✅ 所有依赖已准备就绪，可以打包")
            print("\n下一步：")
            print("1. 打包完成后首次启动时，运行：")
            print("   python scripts/inject_system_manual.py")
            print("2. 这会将系统说明书注入向量库，主Agent可自动检索")
        else:
            print("⚠️  部分依赖缺失，请检查上述错误")
        print("=" * 60)

        return all_ok


def main():
    """主函数"""
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "Niu 个人知识助理 - 依赖打包工具" + " " * 14 + "║")
    print("╚" + "═" * 58 + "╝")

    # 项目根目录
    project_root = Path(__file__).parent.parent

    packager = DependencyPackager(project_root)

    print("\n此脚本将下载所有依赖到本地目录：")
    print(f"  - Python 包: {packager.python_packages_dir}")
    print(f"  - 模型文件: {packager.models_dir}")
    print("\n预计下载大小：")
    print("  - Python 依赖: ~500MB")
    print("  - 人脸识别模型: 326MB")
    print("  - 向量模型: 466MB")
    print("  - 总计: ~1.3GB")

    confirm = input("\n是否继续？ [y/N]: ")
    if confirm.lower() != 'y':
        print("已取消")
        return

    # 执行下载
    packager.download_face_model()
    packager.download_embedding_model()
    packager.install_python_packages()

    # 验证
    packager.verify()


if __name__ == "__main__":
    main()
