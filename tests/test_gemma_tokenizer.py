"""S10 gemma_tokenizer 单测（方案 §5 第 1 条，全 mock：禁真实 LLM、禁写 KG）。

覆盖（逐条对应方案 §5 第 1 条）：
1. encode/decode 往返（decode(encode(x)) == x）；
2. 身份标识 TOKENIZER_ID；
3. 冻结基线样本集（含最坏样本 16×U+3000、中文/英文/病态样本）token 数逐一相等；
4. **decode 返回 str**（回归双审 B-P2 的损坏级错误：写成 List[str] 会让块内容
   变 list、compute_mdhash_id 全错）；
5. 降级：tokenizer.json 不存在/损坏 → 不抛、有 error 日志、get_load_error 有原因。

I-2a 基线语义：CI 断言的是"不变"（测试环境无 llama-server；真机与 /tokenize
逐 ID 对拍是 I-2b，由人工 checklist 确认）。基线变更必须同提交更新并重跑 I-2b。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

import niu_api.internal.gemma_tokenizer as gt


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（gemma_tokenizer 用 loguru 而非 stdlib logging，
    pytest caplog 捕不到——同 tests/test_md_f3.py:24-28 的 per-file 复制模式；
    仓库 conftest 无共享 helper，新文件必须自带）。"""
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


@pytest.fixture
def gemma():
    """真实 Gemma tokenizer 单例（tokenizer.json 已入 git、必在位）。"""
    tokenizer = gt.get_tokenizer()
    assert tokenizer is not None, "tokenizer.json 应在位（已入 git）"
    return tokenizer


# ============== 冻结基线（I-2a） ==============


class TestFrozenBaseline:
    """I-2a：固定样本集 token 数与冻结基线逐一相等。"""

    def test_frozen_baseline_token_counts(self, gemma):
        for sample in gt.FROZEN_ENCODE_BASELINE:
            ids = gemma.encode(sample["text"])
            assert len(ids) == sample["expected_tokens"], (
                f"冻结基线 {sample['name']} 偏离：got {len(ids)}, "
                f"expected {sample['expected_tokens']}。若属有意变更（升级 tokenizers "
                "或更换 tokenizer.json）必须同提交更新基线 + 重跑 I-2b 真机 /tokenize "
                "逐 ID 对拍，禁止为让 CI 绿单独更新基线"
            )

    def test_baseline_covers_worst_case_and_pathological(self):
        """基线必须含最坏样本（16×U+3000 = 48 倍病灶本身）与病态样本。"""
        names = {s["name"] for s in gt.FROZEN_ENCODE_BASELINE}
        assert "worst_16x_u3000" in names, "最坏样本 16×U+3000 缺失（R-9 病灶本身）"
        assert "pathological_16x_nbsp" in names, "病态样本 16×NBSP 缺失"
        worst = next(s for s in gt.FROZEN_ENCODE_BASELINE if s["name"] == "worst_16x_u3000")
        assert worst["text"] == "\u3000" * 16
        assert worst["expected_tokens"] == 48, "16×U+3000 必须是 48 token（48 倍病灶的实测值）"

    def test_encode_returns_list_of_int(self, gemma):
        ids = gemma.encode("你好，世界。")
        assert isinstance(ids, list)
        assert all(isinstance(i, int) for i in ids)


# ============== decode 往返与返回类型 ==============


class TestEncodeDecodeRoundtrip:
    """decode(encode(x)) == x，且 decode 返回 str（B-P2 损坏级错误的回归）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "\u3000" * 16,  # 最坏样本
            "\u00a0" * 16,  # 病态样本
            "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com",
            "The quick brown fox jumps over the lazy dog.",
            "Hello \U0001f44b world \U0001f30d! 中文测试 \U0001f680",
            "line1\nline2\r\n\ttabbed",
            "abcdefghij" * 100,
        ],
    )
    def test_decode_roundtrip(self, gemma, text):
        ids = gemma.encode(text)
        assert gemma.decode(ids) == text

    @pytest.mark.parametrize(
        "text",
        [
            "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com",
            "\u3000" * 16,
            "The quick brown fox jumps over the lazy dog.",
        ],
    )
    def test_decode_returns_str_not_list(self, gemma, text):
        """B-P2 损坏级错误回归：decode 必须返回 str。

        写成 List[str] 会让 lightrag.operate.chunking_by_token_size 的
        ``tokenizer.decode(tokens[...]).strip()`` 拿到 list、块内容变 list、
        compute_mdhash_id 全错——本测试锁死返回类型。
        """
        decoded = gemma.decode(gemma.encode(text))
        assert isinstance(decoded, str), f"decode 必须返回 str，got {type(decoded).__name__}"
        assert not isinstance(decoded, list)

    def test_decode_matches_upstream_signature_semantics(self, gemma):
        """与上游 TokenizerInterface（lightrag/utils.py:1281）行为对齐：
        传 encode 的 token 列表，拿回原文。"""
        text = "2026年10月12日 发布 v1.6 版本。"
        ids = gemma.encode(text)
        assert gemma.decode(ids) == text
        assert gemma.decode(ids) is not None


# ============== 身份标识（I-1 用） ==============


class TestIdentity:
    def test_tokenizer_id(self):
        assert gt.TOKENIZER_ID == "gemma-2"

    def test_instance_carries_id(self, gemma):
        assert gemma.tokenizer_id == gt.TOKENIZER_ID

    def test_tokenizer_json_path_is_repo_file(self):
        """tokenizer.json 路径指向仓库内文件（已入 git，铁律十二）。"""
        assert gt.TOKENIZER_JSON_PATH.is_file(), f"tokenizer.json 应在位: {gt.TOKENIZER_JSON_PATH}"
        assert gt.TOKENIZER_JSON_PATH.stat().st_size > 1_000_000  # 33MB 级，防误指小文件


# ============== 降级（D-3：不抛 + error 日志 + 原因可查） ==============


class TestDegradation:
    """tokenizer.json 缺失/损坏 → 返回 None（不抛）、error 日志、原因可查。"""

    def test_missing_file_returns_none_with_error_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", tmp_path / "does-not-exist.json")
        gt.reset_cache()
        try:
            messages, sink_id = _capture_loguru(level="ERROR")
            try:
                tokenizer = gt.get_tokenizer()
            finally:
                logger.remove(sink_id)

            assert tokenizer is None, "文件缺失必须返回 None 而不是抛异常"
            assert gt.get_load_error() is not None
            assert "不存在" in gt.get_load_error()
            assert any("GemmaTokenizer" in m and "ERROR" in m for m in messages), (
                f"缺失时必须记 error 日志，got: {messages}"
            )
        finally:
            gt.reset_cache()

    def test_corrupt_file_returns_none_with_reason(self, tmp_path, monkeypatch):
        """损坏（能打开但不是合法 tokenizer.json）同样不抛。"""
        bad = tmp_path / "tokenizer.json"
        bad.write_text("{ not a valid tokenizer json", encoding="utf-8")
        monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", bad)
        gt.reset_cache()
        try:
            tokenizer = gt.get_tokenizer()
            assert tokenizer is None
            err = gt.get_load_error()
            assert err is not None
            # 原因含异常类型（JSON 解析/反序列化失败），不是空串
            assert any(t in err for t in ("Exception", "Error")), f"原因应含异常类型: {err}"
        finally:
            gt.reset_cache()

    def test_singleton_caches_failure(self, tmp_path, monkeypatch):
        """失败后单例缓存失败（不重复读盘）；reset_cache 才重试。"""
        monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", tmp_path / "nope.json")
        gt.reset_cache()
        try:
            assert gt.get_tokenizer() is None
            # 不重试：仍是 None 且原因不变
            assert gt.get_tokenizer() is None
            assert gt.get_load_error() is not None
        finally:
            gt.reset_cache()

    def test_recovery_after_reset(self, tmp_path, monkeypatch):
        """reset_cache 后路径恢复即可重新加载（降级不是永久状态）。"""
        real_path = gt.TOKENIZER_JSON_PATH
        monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", tmp_path / "nope.json")
        gt.reset_cache()
        assert gt.get_tokenizer() is None
        # 路径恢复正常 + reset_cache → 能重新加载成功
        monkeypatch.setattr(gt, "TOKENIZER_JSON_PATH", real_path)
        gt.reset_cache()
        try:
            assert gt.get_tokenizer() is not None
        finally:
            gt.reset_cache()


def test_tokenizer_json_path_parent_is_models_dir():
    """路径形态契约：models/tokenizers/embeddinggemma-2/tokenizer.json。"""
    path = Path(gt.TOKENIZER_JSON_PATH)
    assert path.parent.name == "embeddinggemma-2"
    assert path.parent.parent.name == "tokenizers"
    assert path.name == "tokenizer.json"
