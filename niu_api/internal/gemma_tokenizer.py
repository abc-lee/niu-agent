"""S10：Gemma tokenizer —— 让 LightRAG 分段口径与 llama-server 嵌入口径一致。

背景（方案 docs/superpowers/plans/2026-10-12-s10-tokenizer-alignment.md §1.1，
PM 全链实证）：
LightRAG 文档入库按 tiktoken（TiktokenTokenizer，o200k_base）切分，
llama-server 嵌入按 Gemma（GGUF 内建）计数并受 8192 硬上限约束。两把尺子量
同一段文本，最坏差 48 倍（o200k token id 106378 解码为 16 个 U+3000：
tiktoken 记 1 token、Gemma 记 48 token）。1200 tiktoken token 的 chunk 最坏
可达 57,600 Gemma token，被 S7 护栏静默截断（丢内容）。根治＝把分段 tokenizer
换成与嵌入一致的 Gemma。

实现要点：
- 实现 LightRAG 的 TokenizerInterface（lightrag/utils.py:1281），**签名与上游
  逐字一致**：``encode(content: str) -> List[int]``、
  ``decode(tokens: List[int]) -> str``。decode 返回 str——写成 List[str] 会让
  块内容变 list、compute_mdhash_id 全错，双审 B-P2 判定的损坏级错误。
- 内部用 ``tokenizers.Tokenizer.from_file`` 加载
  ``models/tokenizers/embeddinggemma-2/tokenizer.json``（官方 Gemma tokenizer，
  社区镜像 mlx-community/embeddinggemma-300m-8bit；官方 google/embeddinggemma-300m
  为 gated 需登录，两者 tokenizer 与 llama-server 计数一致已实测）。
- encode 一律 ``add_special_tokens=False``——与 llama-server ``/tokenize``
  口径一致（双审 B 角实测：5 样本 × add_special 两态 + 6000 字真实文档共
  3186 token，token ID 逐一致相同，非仅数量相同）。
- 单例加载；加载失败返回 None 并记 error 日志（不抛），失败原因经
  ``get_load_error()`` 暴露，供调用方（lightrag_manager 自检报 ERR /
  lightrag_repair 降级 warning）引用。

不变量锚点（方案 §4）：
- I-1（tokenizer.json 可用时注入）、I-2a（FROZEN_ENCODE_BASELINE 冻结基线）、
- I-2b（真机与 /tokenize 逐 ID 对拍，CI 无 llama-server 不在此断言）。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from loguru import logger

# 模块级身份标识：日志/I-1 断言/unavailable 文案都用它（不硬编码散落字符串）。
TOKENIZER_ID = "gemma-2"

# tokenizer.json 路径：仓库内 models/tokenizers/embeddinggemma-2/（已入 git，
# 铁律十二：运行时要读必须入 git）。按 __file__ 定位仓库根，与 cwd 无关；
# 以模块属性暴露，测试可 monkeypatch 指向不存在路径构造降级场景。
TOKENIZER_JSON_PATH: Path = (
    Path(__file__).resolve().parents[2] / "models" / "tokenizers" / "embeddinggemma-2" / "tokenizer.json"
)

# I-2a 冻结基线样本集（CI 断言"不变"）：固定样本的 Gemma encode token 数与
# 期望值逐一相等。基线值由真机 llama-server /tokenize 逐 ID 对拍确认后冻结。
# 变更流程（方案 §4 I-2a）：CI 失败默认阻断合并；确属有意变更（升级 tokenizers
# 或更换 tokenizer.json）必须①同一提交更新基线并说明理由，②重跑 I-2b 真机
# 与 /tokenize 逐 ID 对拍并贴证据；禁止"为了让 CI 绿"单独更新基线。
# 样本覆盖：最坏样本 16×U+3000（48 倍病灶本身）、16×NBSP（16 倍）、中文、
# 英文、中英数字混合、emoji、换行制表、长 ASCII 串、单字符、全角标点。
FROZEN_ENCODE_BASELINE: tuple[dict[str, Any], ...] = (
    {"name": "empty", "text": "", "expected_tokens": 0},
    {"name": "worst_16x_u3000", "text": "\u3000" * 16, "expected_tokens": 48},
    {"name": "pathological_16x_nbsp", "text": "\u00a0" * 16, "expected_tokens": 32},
    {"name": "chinese_contacts", "text": "通讯录：张三，电话 13800138000，邮箱 zhangsan@example.com", "expected_tokens": 28},
    {"name": "english_fox", "text": "The quick brown fox jumps over the lazy dog.", "expected_tokens": 10},
    {
        "name": "mixed_zh_en_digits",
        "text": "2026年10月12日 发布 v1.6 版本，新增 S10 任务：tokenizer 对齐（accuracy 99.9%）。",
        "expected_tokens": 37,
    },
    {"name": "emoji", "text": "Hello \U0001f44b world \U0001f30d! 中文测试 \U0001f680", "expected_tokens": 11},
    {"name": "newlines_tabs", "text": "line1\nline2\r\n\ttabbed", "expected_tokens": 10},
    {"name": "ascii_long", "text": "abcdefghij" * 100, "expected_tokens": 300},
    {"name": "single_char", "text": "a", "expected_tokens": 1},
    {"name": "fullwidth_punct", "text": "！？。，、；：「」『』（）——……《》", "expected_tokens": 14},
)


class GemmaTokenizer:
    """LightRAG TokenizerInterface 实现（签名与上游逐字一致）。

    ``encode`` 走官方 Gemma tokenizer（add_special_tokens=False，与 llama-server
    ``/tokenize`` 口径一致）；``decode`` 返回 ``str``——lightrag/operate.py:153
    的 chunking_by_token_size 直接 ``tokenizer.decode(tokens[...]).strip()``，
    返回 list 会让块内容变 list、compute_mdhash_id 全错（双审 B-P2 损坏级错误）。
    """

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer
        # S11 D-1：构造期 eager 建 byte-token 表（禁惰性：get_tokenizer() 的单例在持
        # _cache_lock（非可重入）期间构造实例，__init__ 绝不能再碰锁（自锁死）；惰性
        # 构建则无锁竞态 → 双重建表/半成品表，见方案 §5 D-1）。表挂在实例上，
        # reset_cache() 后新实例重建，无模块级表。
        # 表形失败（≠256 项 / 解析异常）→ 置空表 + 恰好一次 WARNING + decode()
        # 退回原路径（INV-4 / AC-6）；实例本身仍可用并被缓存。
        try:
            table = self._build_byte_token_table()
        except Exception:  # noqa: BLE001 —— INV-4：decode 主路径绝不抛异常
            table = {}
            logger.warning("[GemmaTokenizer] byte-token 表构建失败，decode() 退回原行为")
        else:
            count = len(table)
            if count != 256:
                table = {}
                logger.warning(
                    f"[GemmaTokenizer] byte-token 表形非法（应 256 项，实得 {count} 项），"
                    "decode() 退回原行为"
                )
        self._byte_token_table = table

    def __copy__(self) -> GemmaTokenizer:
        """浅拷贝返回自身：tokenizer 构造后不可变（encode/decode 只读），复制无意义。"""
        return self

    def __deepcopy__(self, memo: dict) -> GemmaTokenizer:
        """深拷贝返回自身：LightRAG lightrag.py 的 aquery_data 开头
        ``global_config = asdict(self)`` 每查询深拷贝整对象——拷贝 HF Rust
        tokenizer 实测 1.85s/查询（序列化+反序列化）。而上游 12 处
        ``global_config["tokenizer"]`` 用法全是 encode/decode 只读，
        共享只读实例即可。"""
        return self

    def _build_byte_token_table(self) -> dict[int, int]:
        """S11 D-1：扫描词表，收集字节回退 token，建 token_id → 字节值表。

        形如 ``<0x??>``（长度 6、前 3 字符 ``<0x``、尾字符 ``>``）的 token 恰好
        256 个，覆盖全部字节值（如 U+3000 的组是 ``<0xE3><0x80><0x80>``）。
        两位十六进制实测全为大写（如 ``<0xFE>``）——解析**必须接受大写**
        （``int(s, 16)`` 大小写皆收）；若写成只收小写的正则 ``[0-9a-f]{2}``
        只会匹配出 100 项，漏掉含 A–F 的 156 项，表被判 ≠256 而置空 →
        修复静默失效（S11 §5 D-1）。合法表必须恰好 256 项，否则由调用方
        （``__init__``）降级为空表。
        """
        table: dict[int, int] = {}
        for token_id in range(self._tokenizer.get_vocab_size()):
            text = self._tokenizer.id_to_token(token_id)
            if text is None or len(text) != 6 or text[:3] != "<0x" or text[-1] != ">":
                continue
            table[token_id] = int(text[3:5], 16)
        return table

    @property
    def tokenizer_id(self) -> str:
        """身份标识（日志 / 断言用）。"""
        return TOKENIZER_ID

    def encode(self, content: str) -> list[int]:
        """Encodes a string into a list of tokens（上游 TokenizerInterface 签名）。"""
        return list(self._tokenizer.encode(content, add_special_tokens=False).ids)

    def decode(self, tokens: list[int]) -> str:
        """Decodes a list of tokens into a string（上游 TokenizerInterface 签名）。

        返回 str（不是 List[str]）：块内容经此重建后参与 compute_mdhash_id，
        类型错了就是数据损坏。

        S11 D-2/D-3：快速路径在 raw 结果不含 U+FFFD 时原样返回（INV-1，
        与修复前逐字节相同）；慢路径对头/尾连续 byte-token 段剥离孤儿续字节
        与残缺字符（各 ≤3 token，INV-3），使切片边界落在字节回退字符组
        中间（如 U+3000 = 3 个共享同一 offset 的 token）时，tokenizers 吐出的
        U+FFFD 不再污染 chunk 内容。退化切片（head + tail >= len(tokens)，
        整片全由残缺/孤儿字节构成）→ 返回空串（不是归零——归零会把 U+FFFD
        放回去）。剥离后仍残留 U+FFFD → 一律 WARNING（永不 ERROR：源文本
        本身可含 U+FFFD，方案 R-8），携带 head/tail/raw_count/out_count。
        """
        toks = list(tokens)
        raw = self._tokenizer.decode(toks)
        if "\ufffd" not in raw:
            return raw  # D-2 快速路径：绝大多数切片走这里，字节级零变化

        table = self._byte_token_table
        if not table:
            return raw  # INV-4：表形失败 → 退回原行为

        head = self._leading_orphan_count(toks)
        tail = self._trailing_orphan_count(toks)
        if head + tail >= len(toks):
            return ""  # 退化切片：空串（方案 D-3 / R-9）

        out = self._tokenizer.decode(toks[head : len(toks) - tail])
        if "\ufffd" in out:
            logger.warning(
                f"[GemmaTokenizer] decode 边缘剥离后仍含 U+FFFD: "
                f"head={head} tail={tail} "
                f"raw_count={raw.count(chr(0xFFFD))} out_count={out.count(chr(0xFFFD))}"
            )
        return out

    def _leading_orphan_count(self, toks: list[int]) -> int:
        """D-3 头部：数 tokens 开头连续 byte-token 段中前导的续字节（0x80–0xBF）。

        这些是切片从字符中间截断留下的孤儿续字节。段为空（首 token 不是
        byte-token）→ 显式短路返回 0。
        """
        table = self._byte_token_table
        i = 0
        while i < len(toks) and toks[i] in table:
            if not 0x80 <= table[toks[i]] <= 0xBF:
                break
            i += 1
        return i

    def _trailing_orphan_count(self, toks: list[int]) -> int:
        """D-3 尾部：UTF-8 残缺字符应剥离的 token 数（INV-3：≤3）。

        - 段为空（末 token 不是 byte-token）→ 0（必须显式短路，否则
          按"整段都是续字节"分支取 run[-1] 会 IndexError，方案 R-4）；
        - 整段都是续字节 → 全剥（孤儿前导，如 U+3000 末片 ``[0x80,0x80]``）；
        - 否则看前一个 lead 字节声明的字符长度 L（0x00–0x7F→1、0xC0–0xDF→2、
          0xE0–0xEF→3、0xF0–0xF7→4，非法 lead 记 L=0）：1 + 续字节数 < L
          → 该字符残缺 → 剥离 lead + 续字节；否则尾部完整 → 剥 0。
          续字节数超过 L-1 的"过冲"分支（仅手造切片可达，``encode()`` 产物
          不可达，方案 R-12）不剥离，靠 decode 的 WARNING 兜底。
        """
        table = self._byte_token_table
        n = len(toks)
        i = n - 1
        while i >= 0 and toks[i] in table:
            i -= 1
        run_len = n - 1 - i
        if run_len == 0:
            return 0
        c = 0
        while c < run_len and 0x80 <= table[toks[n - 1 - c]] <= 0xBF:
            c += 1
        if c == run_len:
            return c  # 整段都是孤儿续字节
        lead = table[toks[n - c - 1]]
        if 0x00 <= lead <= 0x7F:
            length = 1
        elif 0xC0 <= lead <= 0xDF:
            length = 2
        elif 0xE0 <= lead <= 0xEF:
            length = 3
        elif 0xF0 <= lead <= 0xF7:
            length = 4
        else:
            length = 0  # 非法 lead：0xF8–0xFF（0x80–0xBF 已计入续字节）
        if length == 0 or 1 + c < length:
            return 1 + c
        return 0


# 单例缓存（tokenizer.json 33MB，每次调用重新加载不可接受）+ 失败原因缓存
# （避免加载失败时每次调用都重打 error 日志 / 重复读盘）。
_tokenizer_cache: GemmaTokenizer | None = None
_load_error: str | None = None
_cache_lock = threading.Lock()


def get_tokenizer() -> GemmaTokenizer | None:
    """单例加载 Gemma tokenizer；失败返回 None 并记 error 日志（不抛）。

    成功路径与失败路径都做单例缓存（失败后 ``reset_cache()`` 才重试），
    供 lightrag_manager 注入 LightRAG 与 lightrag_repair 双口径重切共用。
    """
    global _tokenizer_cache, _load_error

    if _tokenizer_cache is not None:
        return _tokenizer_cache

    with _cache_lock:
        if _tokenizer_cache is not None:
            return _tokenizer_cache
        if _load_error is not None:
            return None

        path = TOKENIZER_JSON_PATH
        if not path.is_file():
            _load_error = f"tokenizer.json 不存在: {path}"
            logger.error(f"[GemmaTokenizer] {_load_error}，分段 tokenizer 将退回 tiktoken")
            return None

        try:
            from tokenizers import Tokenizer as _HFTokenizer

            tokenizer = GemmaTokenizer(_HFTokenizer.from_file(str(path)))
        except Exception as e:  # noqa: BLE001
            _load_error = f"{type(e).__name__}: {e}（{path}）"
            logger.error(f"[GemmaTokenizer] 加载失败: {_load_error}，分段 tokenizer 将退回 tiktoken")
            return None

        _tokenizer_cache = tokenizer
        logger.info(
            f"[GemmaTokenizer] 加载成功（{TOKENIZER_ID}，add_special_tokens=False，"
            f"与 llama-server /tokenize 口径一致）: {path}"
        )
        return _tokenizer_cache


def get_load_error() -> str | None:
    """返回上一次加载失败原因（未失败过返回 None）。

    调用方用它在降级 warning / 自检 ERR 里引用具体原因（缺文件 vs 解析失败）。
    """
    return _load_error


def reset_cache() -> None:
    """清理单例缓存 + 失败原因（测试用：切换 TOKENIZER_JSON_PATH 后重新加载）。"""
    global _tokenizer_cache, _load_error
    with _cache_lock:
        _tokenizer_cache = None
        _load_error = None
