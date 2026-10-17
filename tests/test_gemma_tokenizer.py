"""S10 gemma_tokenizer 单测（方案 §5 第 1 条，全 mock：禁真实 LLM、禁写 KG）。

覆盖（逐条对应方案 §5 第 1 条）：
1. encode/decode 往返（decode(encode(x)) == x）；
2. 身份标识 TOKENIZER_ID；
3. 冻结基线样本集（含最坏样本 16×U+3000、中文/英文/病态样本）token 数逐一相等；
4. **decode 返回 str**（回归双审 B-P2 的损坏级错误：写成 List[str] 会让块内容
   变 list、compute_mdhash_id 全错）；
5. 降级：tokenizer.json 不存在/损坏 → 不抛、有 error 日志、get_load_error 有原因。
6. S11 切片场景：刻意构造的字节回退输入（U+3000/NBSP/U+2003/4 字节字符）按
   LightRAG 真实步进切片——每片 U+FFFD=0、内容保真（退化片豁免）、干净输入
   字节级不变、幂等、源文本自带 U+FFFD 不打 ERROR、表构建失败降级（见文末 S11 段）。
7. 拷贝语义：copy.copy / copy.deepcopy 均返回自身（身份断言，回归 LightRAG
   每查询 asdict 深拷贝整对象的 HF Rust tokenizer 拷贝开销，见 TestCopySemantics）。

I-2a 基线语义：CI 断言的是"不变"（测试环境无 llama-server；真机与 /tokenize
逐 ID 对拍是 I-2b，由人工 checklist 确认）。基线变更必须同提交更新并重跑 I-2b。
"""
from __future__ import annotations

import copy
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


# ============== 拷贝语义（LightRAG asdict 深拷贝回归） ==============


class TestCopySemantics:
    """copy.copy / copy.deepcopy 返回自身。

    依据：LightRAG lightrag.py 的 aquery_data 开头 ``global_config = asdict(self)``
    每查询深拷贝整个 LightRAG 对象，tokenizer 字段随之被深拷贝（拷贝 HF Rust
    tokenizer 实测 1.85s/查询）；而 12 处 ``global_config["tokenizer"]`` 用法
    全是 encode/decode 只读，共享只读实例即可。身份断言（不测时间）捕捉
    "有人删掉 __copy__/__deepcopy__"的回归。
    """

    def test_copy_returns_self(self, gemma):
        assert copy.copy(gemma) is gemma

    def test_deepcopy_returns_self(self, gemma):
        assert copy.deepcopy(gemma) is gemma

    def test_copies_share_encode_decode(self, gemma):
        """深拷贝后 encode/decode 与原件一致（共享只读实例）。"""
        text = "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com"
        for tok in (copy.copy(gemma), copy.deepcopy(gemma)):
            ids = gemma.encode(text)
            assert tok.encode(text) == ids
            assert tok.decode(ids) == text


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


# ============== S11 切片场景用例（方案 §7 Task-2） ==============
#
# 病灶：Gemma 对 U+3000/NBSP/U+2003/4 字节字符走 byte-level BPE 回退
# （一个字符 = 2~4 个共享同一 offset 的 token）；LightRAG 按 token 下标切片
# （range(0, n, 1200-50)，operate.py chunking_by_token_size）后边界落在字符组
# 中间时，旧 decode() 输出 U+FFFD 污染 chunk 内容。S10 的 23 个用例全是短样本
# （不跨切片边界）所以全绿却漏掉缺陷——本组**刻意构造**字节回退输入覆盖每个
# 切片边界（方案 R-6：常见 emoji 😀👋🚀🌍🧑 在本词表是单 token，构造不出缺陷）。
#
# pre-fix 归因（方案 §7 Task-2 订正，不笼统声称"全部 pre-fix 失败"）：
# - 第 1 组显式切片断言（mid-char 内容断言 + 退化片 == "" + 手造残缺切片）：pre-fix 失败；
# - 第 3 组步进扫描 U+FFFD=0：pre-fix 失败（每个输入都至少有一片中段边界）；
# - 第 4 组"含完整字符→非空"：pre-fix 必然通过（FFFD 内容同样非空，属保真护栏）；
#   退化片显式豁免（4 字节字符退化尾片 span 非空而 decode 为空，方案 AC-2b）；
# - 第 5/6 组（干净输入不变、幂等）：pre-fix 必然通过（测"不回归"）；
# - 第 7 组：全序列 "a\ufffdb" 断言 pre-fix 通过（无边界切割）；病态窗口
#   [465,366,366,238479,465] 内容断言 pre-fix 失败、WARNING 断言为修复后新机制；
# - 第 8 组：不抛/原样返回/实例可用 行为上 pre-fix 兼容；"表形失败 WARNING"
#   为修复后新机制（pre-fix 无建表，patch 该私有方法本身不成立）。

CHUNK_SIZE = 1200  # lightrag operate.py chunking_by_token_size 真实配置
CHUNK_STEP = CHUNK_SIZE - 50  # chunk_token_size - chunk_overlap_token_size

# 第 1 组：刻意构造的字节回退输入（每条附构造理由，防后人"简化"成零断言力样本）
_BYTE_FALLBACK_INPUTS = (
    ("\u3000" * 384, "1152 token，末片 [1150:1152] 是 2 token 退化片（h+t>len）"),
    ("\u3000" * 767, "2301 token，第 2 片 [1150:2301] 从 U+3000 字符组中间开始"),
    ("\u3000" * 3000, "9000 token 长段：每步 1150 ≡ 1 (mod 3)，k≥1 的每片头都孤儿"),
    ("a" + "\u00a0" * 700,
     "NBSP=2 token/字符，加奇数前缀 'a' 使 [1150:1401] 落在 0xA0 续字节上；"
     "无前缀则 1150 % 2 == 0 每片都落在字符边界、pre-fix 也通过 = 零断言力（方案 R-6）"),
    ("a" + "\u2003" * 767,
     "U+2003=3 token/字符：700 只够到 1401 token，[1150:1401] 的 1150 恰是字符边界"
     "（零断言力）；767 → 2302 token 使第 3 片 [2300:2302] = [0xE2,0x80] 落在 3 字节"
     "字符中间（尾部残缺）"),
    ("\U0002000B" * 288, "4 字节字符 𠀋（encode=[478,398,366,377]），末片 2 个续字节 = 退化片"),
    ("\U0002000B" * 3000, "4 字节字符长段 12000 token：1150 ≡ 2 (mod 4)，每片头孤儿"),
    ("\U0001FAE8" * 3000, "4 字节 emoji 🫨（encode=[478,397,409,406]）长段"),
)


def _chunks_of(gemma, text):
    """按 LightRAG 真实步进切片，依次产出 (start, tokens, decoded)。"""
    ids = gemma.encode(text)
    for start in range(0, len(ids), CHUNK_STEP):
        tokens = ids[start : start + CHUNK_SIZE]
        yield start, tokens, gemma.decode(tokens)


def _complete_char_ranges(gemma, text):
    """独立 oracle（方案 AC-2 码位粒度，非 token 组粒度——token 组读法会把 128 个
    正确结果误报为违规，R4 B 角实测）：按 tokenizers offsets 把同一码位的 token 分组，
    产出字符 token 区间列表 [(cs, ce), ...]（ce 为开区间端点）。本 oracle 不复用
    decode() 的剥离逻辑，防止测试自证。"""
    offsets = gemma._tokenizer.encode(text, add_special_tokens=False).offsets
    ranges = []
    start = 0
    for i in range(1, len(offsets) + 1):
        if i == len(offsets) or offsets[i] != offsets[i - 1]:
            ranges.append((start, i))
            start = i
    return ranges


def _slice_has_complete_char(ranges, start, end):
    """切片 [start:end) 含完整字符 ⇔ 存在字符区间完全落在切片内。"""
    return any(cs >= start and ce <= end for cs, ce in ranges)


class TestConstructedByteFallbackSlices:
    """第 1 组：刻意构造的字节回退输入的显式切片。内容断言与退化片断言 pre-fix 均失败。"""

    @pytest.mark.parametrize(
        ("text", "start", "expected"),
        [
            # 第 2 片头 2 个孤儿续字节被剥掉 → 恰为完整字符 383 个（U+3000=3 token/字）
            ("\u3000" * 767, 1150, "\u3000" * 383),
            # NBSP 第 2 片头 1 个孤儿续字节 → 完整字符 125 个（2 token/字）
            ("a" + "\u00a0" * 700, 1150, "\u00a0" * 125),
            # U+2003 第 2 片 [1150:2302]（1152 token = 384 个完整字符）：1150 与 2302
            # 都恰为字符边界，无孤儿、raw 本就无 U+FFFD——此条 pre-fix 也通过，属回归护栏
            ("a" + "\u2003" * 767, 1150, "\u2003" * 384),
            # 4 字节字符第 2 片 [1150:2350]：头 2 + 尾 2 个孤儿续字节被剥
            # （2350 % 4 == 2，尾部同样落在字符中间）→ 恰为完整字符 299 个
            ("\U0001FAE8" * 3000, 1150, "\U0001FAE8" * 299),
            ("\U0002000B" * 3000, 1150, "\U0002000B" * 299),
        ],
    )
    def test_mid_character_slice_decodes_to_complete_chars(self, gemma, text, start, expected):
        ids = gemma.encode(text)
        tokens = ids[start : start + CHUNK_SIZE]
        decoded = gemma.decode(tokens)
        assert decoded == expected, (
            f"切片 [start={start}, {len(tokens)} token] 边缘剥离后应恰为完整字符：got len={len(decoded)}，"
            f"expected len={len(expected)}（前缀前 24 码位 {decoded[:24]!r}）"
        )

    @pytest.mark.parametrize(
        ("text", "start"),
        [
            # 末片 [0x80,0x80] 全是孤儿续字节：h=2, t=2, h+t>len
            ("\u3000" * 384, 1150),
            # 𠀋 的末片 [0x80,0x8B] 两个续字节：退化尾片（方案 AC-2b 的 4 字节字符形态）
            ("\U0002000B" * 288, 1150),
            # U+2003 的末片 [0xE2,0x80]：3 字节字符残缺（头 0 + 尾 2，h+t==len）
            ("a" + "\u2003" * 767, 2300),
        ],
    )
    def test_degraded_tail_slices_return_empty_string(self, gemma, text, start):
        """退化切片（整片全由残缺/孤儿字节构成）→ 返回空串，而非"归零"——
        归零会把整片原样解码、把 U+FFFD 放回去（方案 D-3 / R-9）。pre-fix 失败。"""
        ids = gemma.encode(text)
        tokens = ids[start : start + CHUNK_SIZE]
        decoded = gemma.decode(tokens)
        assert decoded == "", (
            f"退化片（{len(tokens)} token，start={start}）应返回空串，got {decoded!r}"
        )

    def test_manually_specified_incomplete_slices(self, gemma):
        """第 2 组：人工指定的头/尾残缺切片（手造，非 encode() 产物，R-12 适用域外）。
        pre-fix 失败（raw 解码吐 U+FFFD 串）。"""
        # [0x80,0xE3,0x80]：头孤儿 1 + 尾 3 字节字符残缺 2，h+t==len → 空串
        assert gemma.decode([366, 465, 366]) == ""
        # [0xE3,0x80]：3 字节字符只到 2 字节 → 尾部残缺剥 2，h+t==len → 空串
        assert gemma.decode([465, 366]) == ""


class TestLightRAGStepSweep:
    """第 3 组：按 LightRAG 真实步进 range(0, n, 1200-50) 扫描第 1 组每个输入，
    断言每片 decode 后 U+FFFD 数为 0。pre-fix 对全部输入失败。"""

    @pytest.mark.parametrize("text", [t for t, _ in _BYTE_FALLBACK_INPUTS])
    def test_every_slice_zero_fffd(self, gemma, text):
        bad = []
        for start, tokens, decoded in _chunks_of(gemma, text):
            if "\ufffd" in decoded:
                bad.append(f"start={start} len={len(tokens)} {decoded[:24]!r}")
        assert not bad, (
            f"{text[:16]!r}…（{len(text)} 字符）存在含 U+FFFD 的切片：{bad}"
        )


class TestContentFidelity:
    """第 4 组：内容保真——含至少一个完整字符的切片 decode 后不得为空串；
    退化片（不含完整字符，h+t>=len 返回 ""）显式豁免（方案 AC-2/AC-2b：4 字节
    字符的退化尾片 span 非空而 decode 为空是设计行为）。非空断言 pre-fix 必然
    通过（FFFD 内容同样非空）——本组是"修复别把内容剥没了"的护栏，不是 FFFFD 探测器。"""

    @pytest.mark.parametrize("text", [t for t, _ in _BYTE_FALLBACK_INPUTS])
    def test_nonempty_when_slice_contains_complete_char(self, gemma, text):
        ranges = _complete_char_ranges(gemma, text)
        ids = gemma.encode(text)
        for start in range(0, len(ids), CHUNK_STEP):
            end = min(start + CHUNK_SIZE, len(ids))
            if not _slice_has_complete_char(ranges, start, end):
                continue  # 退化片显式豁免
            decoded = gemma.decode(ids[start:end])
            assert decoded != "", (
                f"{text[:16]!r}… 切片 [{start}:{end}] 含完整字符但 decode 为空串"
                "（边缘剥离过度）"
            )


class TestCleanInputInvariant:
    """第 5 组：干净输入字节级不变（INV-1 快速路径）——decode(encode(x)) 与
    raw.decode() 原始结果逐字节相同。pre-fix 必然通过（测"不回归"）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "hello world",
            "你好，世界。",
            "The quick brown fox jumps over the lazy dog.",
            "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com",
            "2026年10月12日 发布 v1.6 版本。",
        ],
    )
    def test_clean_text_decode_unchanged(self, gemma, text):
        assert gemma.decode(gemma.encode(text)) == text

    def test_frozen_baseline_samples_decode_unchanged(self, gemma):
        """既有 FROZEN_ENCODE_BASELINE 11 样本：原有 encode 计数断言之外，补 decode
        字节级不变（方案 Task-2 第 5 条）。"""
        for sample in gt.FROZEN_ENCODE_BASELINE:
            assert gemma.decode(gemma.encode(sample["text"])) == sample["text"], (
                f"冻结基线 {sample['name']} 的 decode 结果变化（INV-1 违规）"
            )


class TestDecodeIdempotent:
    """第 6 组：幂等——同一切片两次 decode 结果相同（方案 AC-5 在 decode 层的体现）。
    pre-fix 必然通过（raw 路径同样确定）。"""

    @pytest.mark.parametrize("text", [t for t, _ in _BYTE_FALLBACK_INPUTS])
    def test_same_slice_decoded_twice_is_equal(self, gemma, text):
        for start, tokens, first in _chunks_of(gemma, text):
            assert gemma.decode(tokens) == first, (
                f"{text[:16]!r}… 切片 start={start} 两次 decode 不一致（非幂等）"
            )


class TestSourceTextWithFFFD:
    """第 7 组：源文本自带 U+FFFD（方案 AC-7 / R-8）——词表里确有 6 个 token 单独
    decode 即含 U+FFFD，且 encode("\\ufffd")=[238479]。断言：不产生 ERROR 级日志
    （WARNING 允许），内容正确。"""

    def test_full_sequence_content_correct_and_no_error_log(self, gemma):
        """全序列无边界切割：raw 本就含 U+FFFD 但内容正确，走慢路径后原样返回。
        内容断言与无 ERROR 断言 pre-fix 均通过（pre-fix 不打任何日志）。"""
        messages, sink_id = _capture_loguru(level="ERROR")
        try:
            text = "a\ufffdb"
            decoded = gemma.decode(gemma.encode(text))
        finally:
            logger.remove(sink_id)
        assert decoded == text, f"源文本自带 U+FFFD 时内容必须原样保留，got {decoded!r}"
        assert messages == [], f"源文本自带 U+FFFD 不得产生 ERROR 级日志：{messages}"

    def test_pathological_window_residual_is_warning_not_error(self, gemma):
        """方案 R-8 实测反例 [465,366,366,238479,465] = [0xE3,0x80,0x80,<0xFFFD>,0xE3]：
        h=0, t=1（剥掉尾部孤儿 lead），剥离后残留的 U+FFFD 来自源文本 → 一律
        WARNING（携带 head/tail/raw_count/out_count），永不 ERROR。
        pre-fix：内容断言失败（raw 尾部多一个孤儿 0xE3 → 多一个 U+FFFD）；
        WARNING 断言为修复后新机制。"""
        error_msgs, err_id = _capture_loguru(level="ERROR")
        warn_msgs, warn_id = _capture_loguru(level="WARNING")
        try:
            decoded = gemma.decode([465, 366, 366, 238479, 465])
        finally:
            logger.remove(err_id)
            logger.remove(warn_id)
        assert decoded == "\u3000\ufffd", (
            f"剥离尾部孤儿 lead 后应保留源文本的 U+FFFD，got {decoded!r}"
        )
        assert error_msgs == [], f"残留 U+FFFD 只能 WARNING，不得 ERROR：{error_msgs}"
        assert any("U+FFFD" in m for m in warn_msgs), (
            f"必须打带四个数的 WARNING 日志：{warn_msgs}"
        )


class TestByteTableFailureDegradation:
    """第 8 组：byte-token 表构建失败降级（方案 AC-6 / R-2）——表形非法（≠256 项）
    → 不抛异常、decode() 原样返回（退回 raw 行为，可能含 U+FFFD）、实例仍可用。
    用例触发的是**表形失败**（monkeypatch 建表方法返回畸形表），不是"加载失败"
    （加载层异常是另一条路径 → get_tokenizer() 返 None → 整链退回 tiktoken，AC-9）。
    不抛/原样返回/实例可用 pre-fix 行为上兼容（raw 路径本来就原样返回）；
    "表形失败 WARNING"为修复后新机制。"""

    def test_malformed_table_degrades_without_raising(self, gemma, monkeypatch):
        # 表形非法：只有 3 项（≠256）→ __init__ 置空表 + 恰好一次 WARNING
        monkeypatch.setattr(
            gt.GemmaTokenizer, "_build_byte_token_table",
            lambda self: {465: 0xE3, 366: 0x80, 478: 0xF0},
        )
        messages, sink_id = _capture_loguru(level="WARNING")
        try:
            inst = gt.GemmaTokenizer(gemma._tokenizer)  # 不抛异常
        finally:
            logger.remove(sink_id)
        assert inst._byte_token_table == {}, "表形非法必须置空表（降级）"
        assert any("表形非法" in m or "byte-token" in m for m in messages), (
            f"表形失败必须记一次 WARNING：{messages}"
        )
        # decode 原样返回：raw 解码结果（残缺切片会带 U+FFFD），不剥离、不崩
        raw = inst.decode([366, 465, 366])
        assert "\ufffd" in raw, (
            "表形失败必须退回原行为（raw 返回，残缺切片仍含 U+FFFD），不能静默剥离"
        )
        # 实例仍可用
        assert inst.encode("a") == gemma.encode("a")
        assert inst.tokenizer_id == gt.TOKENIZER_ID
