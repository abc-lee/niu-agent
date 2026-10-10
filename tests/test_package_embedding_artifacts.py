"""供模型脚本的 URL/落点映射与防回归（S1）。

本文件不发起任何网络请求，校验的是 URL 候选顺序、键路径映射，
以及"脚本不得另写一份模型路径"这条防回归约束。
"""

from niu_api.internal import embedding_artifacts as ea


class TestUrlCandidates:
    def test_github_direct_is_first_candidate(self):
        urls = ea.llama_download_urls()
        assert urls[0].startswith("https://github.com/ggml-org/llama.cpp/releases/download/")
        # 回落镜像必须是第二候选，不得作为唯一来源（供应链）
        assert any("gh-proxy.com" in u for u in urls[1:])

    def test_url_contains_build_and_asset(self):
        for u in ea.llama_download_urls():
            assert ea.LLAMA_CPP_BUILD in u
            assert ea.llama_asset_name() in u

    def test_gguf_url_uses_mirror(self):
        assert ea.gguf_download_urls()[0].startswith("https://hf-mirror.com/ggml-org/")


class TestPackagerWiring:
    def test_download_embedding_model_exists_and_uses_artifacts(self):
        """防回归：脚本不得另写一份路径（历史故障：两个脚本各写一份且都 stale）。"""
        import inspect

        from scripts.package_all_dependencies import DependencyPackager

        src = inspect.getsource(DependencyPackager.download_embedding_model)
        assert "embedding_artifacts" in src
        assert "paraphrase-multilingual" not in src  # 旧 stale 目标必须清除
