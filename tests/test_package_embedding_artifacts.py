"""维护者打包工具规格数据文件与纯解析防回归（S1）。

打包规格（资产名/下载源/体积/sha256/人脸落点）已全部迁到
`scripts/artifacts.json`——**仅维护者工具**（scripts/package_all_dependencies.py）
消费，运行时代码（niu_api/、agent/）永不读它。本文件同样只站在维护者面：
不 import 任何运行时模块，不发起任何网络请求，不调用任何下载方法，只调用
脚本的纯解析函数。本文件也不得出现真实 URL 字面量——凡需与下载源比对，一律
从 JSON 取值派生。
"""

import json
import re
from pathlib import Path

from scripts.package_all_dependencies import (
    UnsupportedPlatformError,
    artifacts_file,
    current_platform_key,
    face_model_dir,
    gguf_file,
    llama_server_file,
    load_artifacts,
    resolve_face,
    resolve_gguf,
    resolve_llama_variant,
    resolve_models_dir,
    resolve_mmproj,
)


class TestArtifactsDataFile:
    def test_file_exists_and_parses(self):
        p = artifacts_file()
        assert p.is_file()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        for key in ("_comment", "schema_version", "llama_cpp", "gguf", "mmproj", "face"):
            assert key in data, f"artifacts.json 缺顶层键 {key!r}"

    def test_comment_marks_maintainer_only(self):
        """_comment 必须写明：仅维护者工具用；运行时代码不得读它。"""
        comment = load_artifacts()["_comment"]
        assert "维护者工具" in comment
        assert "运行时代码" in comment
        assert "永不读" in comment

    def test_load_artifacts_matches_raw_json(self):
        raw = json.loads(artifacts_file().read_text(encoding="utf-8"))
        assert load_artifacts() == raw
        assert load_artifacts(artifacts_file()) == raw


class TestResolutionReadsDataFile:
    """脚本解析函数确实从该 JSON 取值（不是自带一份私有常量）。"""

    def test_resolve_llama_variant_expands_build(self):
        spec = load_artifacts()
        build = spec["llama_cpp"]["build"]
        v = resolve_llama_variant(spec, "darwin-x64")
        tpl = spec["llama_cpp"]["platforms"]["darwin-x64"]["cpu"]["asset"]
        assert v["asset"] == tpl.replace("{build}", build)
        assert build in v["asset"]            # {build} 已展开为构建号
        assert "{build}" not in v["asset"]    # 不残留模板占位符
        assert v["binary"] == spec["llama_cpp"]["platforms"]["darwin-x64"]["cpu"]["binary"]
        # 下载 URL 由 JSON 的 sources + 构建号 + 资产名派生（本文件不硬编码任何 URL）
        assert v["urls"] == [f"{base}{build}/{v['asset']}" for base in spec["llama_cpp"]["sources"]]

    def test_resolve_llama_variant_rejects_unknown_platform(self):
        import pytest

        with pytest.raises(KeyError):
            resolve_llama_variant(load_artifacts(), "linux-x64")

    def test_resolve_gguf_matches_data_file(self):
        spec = load_artifacts()
        g = resolve_gguf(spec)
        assert g["filename"] == spec["gguf"]["filename"]
        assert g["size"] == spec["gguf"]["size"]
        assert g["sha256"] == spec["gguf"]["sha256"]
        assert g["urls"] == list(spec["gguf"]["sources"])

    def test_resolve_mmproj_matches_data_file(self):
        spec = load_artifacts()
        m = resolve_mmproj(spec)
        assert m["filename"] == spec["mmproj"]["filename"]
        assert m["sha256"] == spec["mmproj"]["sha256"]
        assert m["urls"] == list(spec["mmproj"]["sources"])

    def test_resolve_face_matches_data_file(self):
        spec = load_artifacts()
        f = resolve_face(spec)
        assert f["archive"] == spec["face"]["archive"]
        assert f["files"] == list(spec["face"]["files"])
        assert f["urls"] == list(spec["face"]["sources"])

    def test_current_platform_key_is_supported_and_declared(self):
        key = current_platform_key()
        assert re.fullmatch(r"(darwin|win32)-(x64|arm64)", key)
        # 纯派生的平台键必须能在数据文件的平台清单里查到（否则解析函数必 KeyError）
        assert key in load_artifacts()["llama_cpp"]["platforms"]


class TestLandingPaths:
    """落点必须与运行时读取路径逐字一致（F9：人脸是双层 models/buffalo_l）。"""

    def test_face_model_dir_is_double_layer(self, tmp_path):
        spec = load_artifacts()
        # F9 落点是双层：models/buffalo_l（修前脚本落单层，重定向机器"下了找不到"）
        assert spec["face"]["install_subdir"] == "models/buffalo_l"
        assert face_model_dir(tmp_path, spec) == tmp_path / "models" / "buffalo_l"
        # 不传 spec 时脚本自读同一数据文件，落点不变
        assert face_model_dir(tmp_path) == face_model_dir(tmp_path, spec)

    def test_gguf_file_under_models_dir(self, tmp_path):
        spec = load_artifacts()
        assert gguf_file(tmp_path, spec) == tmp_path / spec["gguf"]["filename"]

    def test_llama_server_file_layout(self, tmp_path):
        spec = load_artifacts()
        binary = resolve_llama_variant(spec, "darwin-x64")["binary"]
        dirname = spec["llama_cpp"]["dirname"]
        assert llama_server_file(tmp_path, "darwin-x64", spec) == tmp_path / dirname / "darwin-x64" / binary

    def test_resolve_models_dir_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NIU_MODELS_PATH", str(tmp_path / "redirected"))
        assert resolve_models_dir(tmp_path) == tmp_path / "redirected"
        monkeypatch.delenv("NIU_MODELS_PATH")
        assert resolve_models_dir(tmp_path) == tmp_path / "models"


class TestPackagerWiring:
    def test_download_embedding_model_uses_resolved_spec(self):
        """防回归：脚本不得另写一份规格（历史故障：两处各写一份且都 stale）。
        现规格唯一来源是 artifacts.json——下载方法必须走解析函数/实例规格，
        不得自带资产名或旧 stale 模型目标。
        """
        import inspect

        from scripts.package_all_dependencies import DependencyPackager

        src = inspect.getsource(DependencyPackager.download_embedding_model)
        assert "self.spec" in src or "resolve_" in src
        assert "paraphrase-multilingual" not in src  # 旧 stale 目标必须清除
