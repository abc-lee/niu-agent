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
        """
        return self._tokenizer.decode(list(tokens))


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
