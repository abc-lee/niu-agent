#!/usr/bin/env python3
"""
完整依赖打包脚本（维护者工具，不属运行时代码）

用途：下载所有依赖（Python 包 + 模型文件）到本地目录
确保打包时所有内容都在项目中，无需运行时下载

下载规格（llama.cpp 资产名/下载源、GGUF·mmproj 文件名/体积/sha256、
buffalo_l 下载源与解压后布局）全部读自与本脚本同目录的规格数据文件
`scripts/artifacts.json`——运行时代码（niu_api/、agent/）永不读该文件
（运行时只读家目录布局文件 ~/.niu/llama-layout.json）。换构建/下载源
只改那份 JSON；把资产名/URL/构建号写回任何代码都是实施错误。

布局事实（目录名/二进制名/文件名/平台清单）须与运行时 ~/.niu/llama-layout.json
保持一致，否则会出现"下了找不到"。人脸模型落点与向量产物同源同一个
models_dir（认 NIU_MODELS_PATH），落点 `<models_dir>/models/buffalo_l`
与程序 get_face_model 的真实读取路径逐字一致（F9：修前脚本落单层
`<models_dir>/buffalo_l`，重定向机器上"下了找不到"）。
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from tqdm import tqdm

# ---------------- 规格数据文件（仅本工具消费） ----------------


def artifacts_file() -> Path:
    """与脚本同目录的规格数据文件：以脚本自身位置为基准，不做 cwd 式查找。"""
    return Path(__file__).resolve().parent / "artifacts.json"


def load_artifacts(path: str | Path | None = None) -> dict:
    """纯读规格数据文件（零下载、零建目录，可安全在 import 期调用）。"""
    p = Path(path) if path is not None else artifacts_file()
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"artifacts.json 顶层必须是 JSON 对象：{p}")
    return data


class UnsupportedPlatformError(RuntimeError):
    """OS 层真不支持（非 darwin/win32）——显式失败，禁止静默回退到错误架构的二进制。"""


def current_platform_key() -> str:
    """平台键 `<sys.platform>-<arch>`：纯由 OS/架构派生，不依赖任何规格数据。"""
    if sys.platform not in ("darwin", "win32"):
        raise UnsupportedPlatformError(
            f"打包工具未适配平台 {sys.platform!r}（仅支持 darwin/win32）"
        )
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{sys.platform}-{arch}"


def resolve_models_dir(project_root: Path) -> Path:
    """模型根目录，与运行时 get_models_dir() 同口径：优先环境变量 NIU_MODELS_PATH，
    否则 <项目根>/models。

    人脸模型落点必须与向量产物共用这同一个 models_dir（F9）：另造一个家目录
    推导会让重定向机器"脚本下到一处、程序到另一处找"。
    """
    env = os.environ.get("NIU_MODELS_PATH")
    if env:
        return Path(env)
    return project_root / "models"


def resolve_llama_variant(spec: dict, platform_key: str, variant: str = "cpu") -> dict:
    """取某平台+变体的 llama.cpp 资产规格：{build, asset, top_dir, binary, urls, needs_exec_bit}。

    asset/top_dir 是模板，`{build}` 展开为规格里的构建号；top_dir=None 表示该归档
    没有顶层目录（实测：macOS tarball 带顶层目录，win32 zip 不带）。
    """
    lc = spec.get("llama_cpp") or {}
    platforms = lc.get("platforms") or {}
    plat = platforms.get(platform_key)
    if not isinstance(plat, dict):
        raise KeyError(
            f"artifacts.json 的 llama_cpp.platforms 缺平台 {platform_key!r}"
            f"（已声明：{sorted(platforms)}）"
        )
    var = plat.get(variant)
    if not isinstance(var, dict):
        raise KeyError(f"平台 {platform_key} 缺变体 {variant!r}（已声明：{sorted(plat)}）")
    build = str(lc.get("build") or "")
    asset = var["asset"].format(build=build)
    top_dir = var.get("top_dir")
    if top_dir:
        top_dir = top_dir.format(build=build)
    return {
        "build": build,
        "asset": asset,
        "top_dir": top_dir,
        "binary": var["binary"],
        "urls": [f"{base}{build}/{asset}" for base in lc.get("sources") or []],
        # Windows 无可执行位概念（与运行时同口径：sys.platform != "win32"）
        "needs_exec_bit": sys.platform != "win32",
    }


def resolve_gguf(spec: dict) -> dict:
    """GGUF 权重规格：{filename, size, sha256, urls}（size 为 None ⇒ 不做期望体积比对）。"""
    g = spec.get("gguf") or {}
    return {
        "filename": g["filename"],
        "size": g.get("size"),
        "sha256": g.get("sha256"),
        "urls": list(g.get("sources") or []),
    }


def resolve_mmproj(spec: dict) -> dict:
    """mmproj 媒体投影规格：{filename, sha256, urls}（维护者工具预留；运行时零下载）。"""
    m = spec.get("mmproj") or {}
    return {
        "filename": m["filename"],
        "sha256": m.get("sha256"),
        "urls": list(m.get("sources") or []),
    }


def resolve_face(spec: dict) -> dict:
    """人脸模型（buffalo_l）规格：{name, archive, top_dir, install_subdir, files, urls}。

    install_subdir 是相对 models_dir 的落点：`models/buffalo_l`（双层），
    与程序读取路径 `get_models_dir()/models/buffalo_l` 逐字一致（F9）。
    """
    f = spec.get("face") or {}
    return {
        "name": f.get("name") or "buffalo_l",
        "archive": f["archive"],
        "top_dir": f.get("top_dir"),
        "install_subdir": f.get("install_subdir") or "models/buffalo_l",
        "files": list(f.get("files") or []),
        "urls": list(f.get("sources") or []),
    }


def face_model_dir(models_dir: Path, spec: dict | None = None) -> Path:
    """人脸模型落点：`<models_dir>/<install_subdir>`（缺省双层 models/buffalo_l）。"""
    spec = spec if spec is not None else load_artifacts()
    return models_dir / resolve_face(spec)["install_subdir"]


def gguf_file(models_dir: Path, spec: dict | None = None) -> Path:
    """GGUF 权重落点：`<models_dir>/<文件名>`。"""
    spec = spec if spec is not None else load_artifacts()
    return models_dir / resolve_gguf(spec)["filename"]


def llama_server_file(models_dir: Path, platform_key: str | None = None,
                      spec: dict | None = None, variant: str = "cpu") -> Path:
    """llama-server 二进制落点：`<models_dir>/<dirname>/<平台键>/<二进制名>`。"""
    spec = spec if spec is not None else load_artifacts()
    platform_key = platform_key or current_platform_key()
    dirname = (spec.get("llama_cpp") or {}).get("dirname") or "llama.cpp"
    binary = resolve_llama_variant(spec, platform_key, variant)["binary"]
    return models_dir / dirname / platform_key / binary


class DependencyPackager:
    """依赖打包器（纯维护流程；下载规格全部来自 artifacts.json）"""

    def __init__(self, project_root: Path, spec: dict | None = None):
        self.project_root = project_root
        self.spec = spec if spec is not None else load_artifacts()
        # 模型目录与运行时同口径（F9）：认 NIU_MODELS_PATH，否则项目根/models
        self.models_dir = resolve_models_dir(project_root)
        self.python_packages_dir = project_root / "python_packages"

        # 创建目录（仅构造时发生；import 本模块本身零副作用）
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.python_packages_dir.mkdir(parents=True, exist_ok=True)

    def download_face_model(self):
        """下载人脸识别模型（buffalo_l）。

        落点 = models_dir/models/buffalo_l（双层，与程序 get_face_model 读取的
        目录相同，F9）；归档的顶层目录（top_dir）解到 models_dir/models/ 下
        即恰好落位。
        """
        face = resolve_face(self.spec)
        model_dir = self.models_dir / face["install_subdir"]
        model_dir.mkdir(parents=True, exist_ok=True)

        # 检查是否已存在（解压后布局以规格 files 为准）
        required_files = face["files"]

        all_exist = all((model_dir / f).exists() for f in required_files)
        if all_exist:
            print("✅ 人脸识别模型已存在，跳过下载")
            return

        zip_path = self.models_dir / face["archive"]
        extract_root = model_dir.parent  # models_dir/models/ —— 归档顶层目录落在这里

        downloaded = False
        for url in face["urls"]:
            print(f"下载地址: {url}")
            print(f"保存到: {zip_path}")
            try:
                with tqdm(unit='B', unit_scale=True, desc="下载中") as pbar:
                    urllib.request.urlretrieve(
                        url,
                        zip_path,
                        reporthook=lambda b, bsize, t: pbar.update(bsize)
                    )

                print("\n解压中...")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    for m in zip_ref.namelist():
                        # 防 zip 路径穿越（第三方镜像供应链）
                        if ".." in m or m.startswith("/"):
                            raise ValueError(f"非法条目: {m}")
                    zip_ref.extractall(extract_root)

                # 清理
                zip_path.unlink()
                downloaded = True
                break

            except Exception as e:
                print(f"❌ 下载失败({url}): {e}")

        if downloaded:
            print(f"✅ 人脸识别模型已下载到: {model_dir}")
        else:
            print("❌ 人脸识别模型下载失败（已尝试全部来源）")
            print("\n请手动下载：")
            if face["urls"]:
                print(f"1. 访问: {face['urls'][0]}")
            print(f"2. 下载后解压到: {extract_root}（其下直接出现 {face['name']} 目录，不要多套一层）")

    @staticmethod
    def _size_warning(path: Path, expected) -> None:
        """体积校验可选化（方案 v1.7）：体积不符仅 WARNING（可能换了量化、或文件
        不完整），仍按可用处理——绝不因不同量化版本判"残缺"。"""
        if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
            return
        actual = path.stat().st_size
        if actual != expected:
            print(f"⚠️ 体积不符（期望 {expected}，实际 {actual}）：可能换了量化、或文件不完整；仍按可用处理")

    def download_embedding_model(self, variant: str = "cpu") -> bool:
        """下载 EmbeddingGemma 2 产物：GGUF 权重 + 当前平台 llama.cpp 二进制。

        下载规格（资产名/下载源/期望体积）来自本脚本同目录的 artifacts.json；
        布局事实（目录名/文件名/平台清单）须与运行时 ~/.niu/llama-layout.json
        一致，否则"下了找不到"。已存在且非空的产物直接跳过，不重复下载；
        重下只由"文件缺失"或"零字节"触发（不存在 --force 开关，全案未提供该
        入口）；体积不符仅 WARNING（v1.7 体积校验可选化）。
        """
        spec = self.spec
        ok = True

        # 平台探测必须在任何下载之前（R2-④）：否则 Linux 等未适配平台会先下完
        # 约 310 MB GGUF 才报错退出，与"未适配显式失败"的意图相悖。
        try:
            plat = current_platform_key()
        except UnsupportedPlatformError as e:
            print(f"❌ {e}")
            return False

        llama = resolve_llama_variant(spec, plat, variant)
        llama_dirname = (spec.get("llama_cpp") or {}).get("dirname") or "llama.cpp"

        # --- GGUF 权重 ---
        gguf = resolve_gguf(spec)
        target = self.models_dir / gguf["filename"]
        if target.is_file() and target.stat().st_size > 0:
            self._size_warning(target, gguf["size"])
            print(f"✅ 向量权重已存在: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            got = False
            for url in gguf["urls"]:
                print(f"下载 {url}")
                try:
                    # 先落临时文件，校验通过再原子改名——避免残损文件被当成成品
                    tmp = target.with_suffix(target.suffix + ".part")
                    urllib.request.urlretrieve(url, tmp)
                    if tmp.is_file() and tmp.stat().st_size > 0:
                        self._size_warning(tmp, gguf["size"])
                        tmp.replace(target)
                        got = True
                        break
                    tmp.unlink(missing_ok=True)
                    print(f"⚠️ 空文件或截断，已丢弃: {url}")
                except Exception as e:  # noqa: BLE001 —— 逐个源尝试，失败即回落下一个
                    print(f"⚠️ 下载失败({url}): {e}")
            if got:
                print(f"✅ 向量权重已就位: {target}")
            else:
                print("❌ GGUF 权重下载失败（已尝试全部来源）")
                ok = False

        # --- llama.cpp 二进制（按平台；plat 已在方法入口探测） ---
        server = self.models_dir / llama_dirname / plat / llama["binary"]
        if server.is_file():
            print(f"✅ llama-server 已存在: {server}")
        else:
            dest = server.parent
            dest.mkdir(parents=True, exist_ok=True)
            done = False
            for url in llama["urls"]:
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

                    # 扁平化顶层目录（R1-P1）：规格的 top_dir 是归档内顶层目录名
                    # （模板已展开）；实测 macOS tarball 成员**带**顶层目录（llama-server
                    # 在其下），Windows zip **无**顶层目录（top_dir 为 null）。若只查
                    # dest/llama-server，macOS 下恒 False，两个来源各白下一遍后失败、
                    # chmod 永不执行——单平台才暴露的不对称缺陷。
                    if llama["top_dir"]:
                        inner = dest / llama["top_dir"]
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
            if llama["needs_exec_bit"]:
                # macOS/Linux 需要可执行位；extractall 不保证保留
                server.chmod(server.stat().st_mode | 0o111)
            print(f"✅ llama-server 已就位: {server}")

        if ok:
            # 最终核对：与运行时缺失检测同口径（仅 is_file），工具与程序对"齐没齐"
            # 不再有两套说法
            missing = [p for p in (target, server) if not p.is_file()]
            if missing:
                print("⚠️ 仍有缺失产物：")
                for p in missing:
                    print(f"  - {p}")
                print("获取与自检办法见《系统管理手册》『可选组件安装』")
                return False
        return True

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

        # 1. 检查人脸识别模型（落点与程序读取路径一致：models_dir/models/buffalo_l，F9）
        face = resolve_face(self.spec)
        face_model_dir = self.models_dir / face["install_subdir"]
        required_files = face["files"]

        face_ok = all((face_model_dir / f).exists() for f in required_files)
        print(f"{'✅' if face_ok else '❌'} 人脸识别模型: {face_model_dir}")

        # 2. 检查向量模型产物（R2-①：保留 embedding_ok 变量名——汇总行仍引用它）
        #    与运行时同口径：仅 is_file；路径按同一布局事实（dirname/平台键/二进制名）派生
        try:
            plat = current_platform_key()
        except UnsupportedPlatformError as e:
            print(f"⚠️ 跳过向量模型检查（{e}）")
            embedding_ok = False
        else:
            gguf_target = gguf_file(self.models_dir, self.spec)
            server = llama_server_file(self.models_dir, plat, self.spec)
            embedding_ok = gguf_target.is_file() and server.is_file()
            if embedding_ok:
                print(f"✅ 向量模型: {gguf_target}")
                print(f"✅ llama-server: {server}")
            else:
                missing = [p for p in (gguf_target, server) if not p.is_file()]
                print(f"❌ 向量模型产物缺失: {'、'.join(str(p) for p in missing)}")

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
    parser = argparse.ArgumentParser(
        description="Niu 依赖打包工具（下载规格取自同目录 artifacts.json）")
    parser.add_argument("--variant", default="cpu",
                        help="llama.cpp 构建变体（缺省 cpu；cuda 等其它变体需先在 artifacts.json 声明）")
    args = parser.parse_args()

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
    if not packager.download_embedding_model(args.variant):
        print("\n❌ 向量模型产物未就绪，已中止（避免把半成品 models/ 打进安装包）。")
        sys.exit(1)
    packager.install_python_packages()

    # 验证
    packager.verify()


if __name__ == "__main__":
    main()
