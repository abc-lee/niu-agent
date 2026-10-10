#!/usr/bin/env python3
"""
完整依赖打包脚本

用途：下载所有依赖（Python 包 + 模型文件）到本地目录
确保打包时所有内容都在项目中，无需运行时下载
"""

import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

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

    def download_embedding_model(self) -> bool:
        """下载 EmbeddingGemma 2 产物：GGUF 权重 + 当前平台 llama.cpp 二进制。

        产物路径的唯一真相源是 niu_api.internal.embedding_artifacts，本方法不得另写路径。
        已存在且体积相符的产物直接跳过，不重复下载；重下只由"文件缺失"或
        "体积不符"触发（不存在 --force 开关，全案未提供该入口）。
        """
        import shutil
        import tarfile
        import urllib.request
        import zipfile

        # sys.path 引导（R1-P1）：`python scripts/package_all_dependencies.py` 的
        # sys.path[0] 是 scripts/，而 niu_api 未装进 site-packages，不引导必
        # ModuleNotFoundError —— 且崩在已下完数百 MB 人脸模型之后。
        # 写法对齐同目录既有脚本（scripts/ingest_unified.py 等）。
        _repo_root = str(Path(__file__).resolve().parent.parent)
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)

        from niu_api.internal.embedding_artifacts import (
            LLAMA_CPP_BUILD,
            current_platform,
            gguf_download_urls,
            gguf_path,
            llama_download_urls,
            llama_needs_exec_bit,
            llama_server_path,
            platform_dir,
            verify_gguf,
        )

        ok = True

        # 平台探测必须在任何下载之前（R2-④）：否则 Linux 等未适配平台会先下完
        # 约 310 MB GGUF 才报错退出，与"未适配显式失败"的意图相悖。
        try:
            plat = current_platform()
        except NotImplementedError as e:
            print(f"❌ {e}")
            return False

        # --- GGUF 权重 ---
        target = gguf_path()
        if target.is_file() and verify_gguf(target):
            print(f"✅ 向量权重已存在: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            got = False
            for url in gguf_download_urls():
                print(f"下载 {url}")
                try:
                    # 先落临时文件，校验通过再原子改名——避免残损文件被当成成品
                    tmp = target.with_suffix(target.suffix + ".part")
                    urllib.request.urlretrieve(url, tmp)
                    if verify_gguf(tmp):
                        tmp.replace(target)
                        got = True
                        break
                    tmp.unlink(missing_ok=True)
                    print(f"⚠️ 体积不符，已丢弃: {url}")
                except Exception as e:  # noqa: BLE001 —— 逐个源尝试，失败即回落下一个
                    print(f"⚠️ 下载失败({url}): {e}")
            if got:
                print(f"✅ 向量权重已就位: {target}")
            else:
                print("❌ GGUF 权重下载失败（已尝试全部来源）")
                ok = False

        # --- llama.cpp 二进制（按平台；plat 已在方法入口探测） ---
        server = llama_server_path()
        if server.is_file():
            print(f"✅ llama-server 已存在: {server}")
        else:
            dest = platform_dir()
            dest.mkdir(parents=True, exist_ok=True)
            asset_urls = llama_download_urls()
            done = False
            for url in asset_urls:
                print(f"下载 {url}")
                try:
                    archive = dest / Path(url).name
                    urllib.request.urlretrieve(url, archive)
                    if url.endswith(".zip"):
                        with zipfile.ZipFile(archive) as zf:
                            for m in zf.namelist():
                                # 防 zip 路径穿越（第三方镜像供应链，R1-⑧）
                                if ".." in m or m.startswith("/"):
                                    raise ValueError(f"非法条目: {m}")
                            zf.extractall(dest)
                    else:
                        with tarfile.open(archive, "r:gz") as tf:
                            base = dest.resolve()
                            for m in tf.getmembers():
                                # 防 tar 路径穿越；python 3.11 无 filter= 参数，手工校验
                                if not (base / m.name).resolve().is_relative_to(base):
                                    raise ValueError(f"非法 tar 条目: {m.name}")
                                # R2-②：链接目标也要在包内。实测 b11540 tarball 确含大量
                                # symlink 成员；只查成员名是词法检查——先落一个指向包外的
                                # symlink，后续成员经它即可写出 dest 之外。
                                if m.issym():
                                    link_target = (base / m.name).parent.joinpath(
                                        m.linkname
                                    ).resolve()
                                    if not link_target.is_relative_to(base):
                                        raise ValueError(f"非法链接目标: {m.linkname}")
                            tf.extractall(dest)
                    archive.unlink(missing_ok=True)

                    # 扁平化顶层目录（R1-P1）：实测 b11540 macOS tarball 成员**带**
                    # 顶层目录 `llama-b11540/`（llama-server 在其下），而 Windows zip
                    # **无**顶层目录。若直接查 dest/llama-server，macOS 下恒 False，
                    # 两个来源各白下一遍后失败、chmod 永不执行 —— 单平台才暴露的不对称缺陷。
                    inner = dest / f"llama-{LLAMA_CPP_BUILD}"
                    if inner.is_dir():
                        for item in list(inner.iterdir()):
                            shutil.move(str(item), str(dest / item.name))
                        inner.rmdir()

                    done = server.is_file()
                    if done:
                        break
                    print(f"⚠️ 解包后未找到 {server.name}，尝试下一来源")
                except Exception as e:  # noqa: BLE001
                    print(f"⚠️ 下载/解包失败({url}): {e}")
            if not done:
                print(f"❌ llama.cpp 二进制获取失败（平台 {plat}，已尝试全部来源）")
                return False
            if llama_needs_exec_bit():
                # macOS/Linux 需要可执行位；extractall 不保证保留
                server.chmod(server.stat().st_mode | 0o111)
            print(f"✅ llama-server 已就位: {server}")

        if ok:
            from niu_api.internal.embedding_artifacts import describe_missing
            left = describe_missing()
            if left:
                print(f"⚠️ 仍有缺失产物：\n{left}")
                return False
        return ok

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
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)

            if result.returncode == 0:
                print(f"✅ Python 包已下载到: {self.python_packages_dir}")

                # 统计文件
                files = list(self.python_packages_dir.glob("*.whl"))
                print(f"   共 {len(files)} 个 wheel 文件")
            else:
                print("❌ 下载失败:")
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

        # 2. 检查向量模型产物（R2-①：保留 embedding_ok 变量名——汇总行仍引用它）
        from niu_api.internal.embedding_artifacts import (
            artifacts_ready,
            describe_missing,
            gguf_path,
            llama_server_path,
        )

        embedding_ok = artifacts_ready()
        if embedding_ok:
            print(f"✅ 向量模型: {gguf_path()}")
            print(f"✅ llama-server: {llama_server_path()}")
        else:
            print(f"❌ 向量模型产物缺失:\n{describe_missing()}")

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
    print("  - 向量模型产物: GGUF 权重约 310MB + llama.cpp 二进制约 11-18MB（按平台）")
    print("  - 总计: ~1.2GB")

    confirm = input("\n是否继续？ [y/N]: ")
    if confirm.lower() != 'y':
        print("已取消")
        return

    # 执行下载
    packager.download_face_model()
    if not packager.download_embedding_model():
        print("\n❌ 向量模型产物未就绪，已中止（避免把半成品 models/ 打进安装包）。")
        sys.exit(1)
    packager.install_python_packages()

    # 验证
    packager.verify()


if __name__ == "__main__":
    main()
