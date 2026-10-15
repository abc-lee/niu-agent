"""
LightRAG Adapter & Ingester

Provides two interfaces to the LightRAG instance:
- LightRAGAdapter: query interface (replaces vector-store search + kg-server query)
- LightRAGIngester: injection via lightrag_insert (auto extraction),
                    inject_custom_kg (precise control), and inject_document (unstructured)

Both delegate to lightrag_manager for the LightRAG instance and use
call_async() for the async/sync bridge.
"""

import re
from typing import Any

from loguru import logger

from niu_api.internal.lightrag_manager import (
    call_async,
    get_lightrag,
    graph_read_lock,
    lightrag_timeout,
)

# Valid query modes for LightRAG
VALID_MODES = {"naive", "local", "global", "hybrid", "mix", "bypass"}

# 图查询结果最大字符数（参考 Claude Code Grep 工具上限）
# explore_node / get_graph_snapshot 在工具层截断，避免 depth=3 limit=100 返回
# 50 万字符触发单消息超限。与 disk 保底截断（30K）形成双层防护。
LIGHTRAG_GRAPH_MAX_CHARS = 20000

# LightRAG's fail_response constant substring markers.
# Used to detect the canned error text that LightRAG returns when queries
# produce no results.  These are not LLM-generated responses — they are
# hard-coded fallback strings that must never leak into system prompts.
_LIGHTRAG_ERROR_MARKERS = ("not able to provide", "[no-context]")

# 门控拒绝通用文案（D5：不细分修复中/损坏/冷却原因——门控 warning 日志已含具体原因）
_GRAPH_UNAVAILABLE_MSG = "知识图谱不可用（初始化门控拒绝）"

# ── T3：custom-KG 入库超限守卫（方案 2026-10-11-s8，§2 D-3/D-3.1）──────────────
# 病灶：LightRAG 结构化写入会把三类文本都嵌入 vdb（chunk 正文 / 实体描述 /
# 关系描述），全部经 encode_batch → llama-server（Gemma，单条上限 8192 token）。
# 超限文本此前只能靠 S7 护栏在嵌入时按比例截断——内容静默丢失。本守卫把失败形态
# 前移到入库前：chunk 分段、description fail-loud。

# 三阈值均派生自 llama_config 唯一真相（本模块不再持有常量）：embed_token_limit()
# （＝生效行里的 -c 上下文大小）、char_prefilter_adapter()（×2000/8192）、
# split_char_budget_adapter()（×1500/8192）；llama-server 侧同样从 llama_config
# 派生（预筛系数 7000/8192，与 lightrag 侧 2000 不同、不得合并）——改生效行 -c
# 全链同步，两侧口径永不分裂。
#
# 字符预筛阈值（char_prefilter_adapter()）：≤ 该长度的组合串直接放行、不做权威
# 计量。依据（方案 §5 R-3）：Gemma 分词器最坏密度 4.000 token/字符（未分配码位
# 2.000；CJK 扩展 B/C 4.000；UTF-8 每字符最多 4 字节，故 4.0 是可辩护上界）。
# 派生值恒低于 embed_token_limit()（基线 8192 下 2000 × 4.000 = 8000 < 8192，
# 随生效 -c 同步缩放）。**抬高该系数必须同步重新评估此上界**（7000 × 4.0 =
# 28000 在基线已超限，v1.2 双审 B-P3 实测证实 7000 不安全）。
#
# 分段子块字符预算（split_char_budget_adapter()）：每子块 ≤ 该预算 ⇒ 最坏不超限
# （基线 1500 × 4.000 = 6000 token < 8192，随生效 -c 同步缩放），切分后的子块
# 无需再次计量。

# 实体名长度上界（D-3.1 快速失败层）：实体名没有任何长度/格式校验（代码里的
# kebab 正则只用于 agent 名），却会原样进入嵌入串（create/edit 的
# `name + "\n" + desc`，以及改名级联里复制进每条关联关系的 src/tgt）。实体名
# 没有正当理由长到几百字符，设保守上界先挡明显异常输入；级联边数极多时它比
# 逐条计量更廉价。**它与主层（逐条计量级联串）叠加生效、不互相替代**——
# 只实现它挡不住"短实体名 + 长存储侧描述"（方案 §4 I-13）。
_ENTITY_NAME_MAX_CHARS = 200


class EmbedLimitError(RuntimeError):
    """T3 守卫 fail-loud：实际嵌入组合串超出 llama-server 8192 token 上限。

    拆分单一实体/关系在语义上不成立；静默截断会直接抹掉路由信号（动态注入消费的
    正是 description）。故直接 raise（报文带实体名/关系名 + 真实 token 数，I-10），
    让调用方（Agent / region 管理器）自己修输入。
    """


# ── T3 覆盖白名单（方案 §4 I-12 ／ §6 Task-4）──────────────────────────────────
# 裁定方式：人工读 diff 逐条比对下面两组名单与方案 D-3 覆盖表；不用 grep 推导
# （v1.4 已废弃 grep 判据：rag.a* 会命中只读查询与删除，按字面不可满足）。
#
# ① 入口调用 _guard_embed_text 的六个写入方法（= D-3 覆盖表全集，无第七个）：
#    1. LightRAGIngester.inject_custom_kg —— chunk 分段 + 实体侧
#       `entity_name + "\n" + description` + 关系侧
#       `keywords + "\t" + src_id + "\n" + tgt_id + "\n" + description`
#    2. LightRAGAdapter.create_entity —— `entity_name + "\n" + description`
#    3. LightRAGAdapter.create_relation ——
#       `keywords + "\t" + src + "\n" + tgt + "\n" + description`
#    4. LightRAGAdapter.edit_entity —— 直接输入 `entity_name + "\n" + description`
#       （改名时用新名、合流分支同时按旧名）+ allow_rename 改名时逐条计量每条
#       关联关系的级联重嵌串
#    5. LightRAGAdapter.edit_relation ——
#       `source + "\t" + target + "\n" + keywords + "\n" + description`
#       （keywords 在第三段，与 1/3 不同，v1.6 双审 A-P1）
#    6. LightRAGAdapter.merge_entities —— `target_entity + "\n" + description`
#       + 源/目标两侧实体每条关系的重嵌
#       `keywords + "\t" + src + "\n" + tgt + "\n" + description`
#
# ② 不设守卫的三个 doc 路径方法：LightRAGIngester.lightrag_insert /
#    inject_document / inject_documents。它们**不是已豁免、不是已安全**，而是
#    **本期未处置的已知残留，见方案 §5 R-9**。摘要：LightRAG 分段按 tiktoken
#    （gpt-4o-mini 的 o200k_base，chunk_token_size=1200 token），llama-server
#    嵌入按 Gemma（上限 8192 token），PM 全词表穷尽扫描实测最坏偏离比 48.0x
#    （o200k id 106378 = 16 个 U+3000：tiktoken 记 1 token、Gemma 记 48 token），
#    1200 token 的 chunk 最坏可达 57,600 Gemma token，会被 S7 护栏静默截断；
#    doc 路径上的 LLM 抽取描述（summary_max_tokens=1200，同一 tiktoken 口径）
#    背离同型。本方案不为它背书，施工时不得跳过它的防护设计。
def _guard_embed_text(kind: str, name: str, text: str) -> list[str] | None:
    """唯一守卫：预筛（char_prefilter_adapter 阈值）→ /tokenize 权威计量 → 超限按 kind 分治。

    Args:
        kind: "chunk"（正文，可分段）／"entity"／"relation"（description 类，
            拆分无意义，fail-loud）。
        name: 实体名/关系名，仅用于超限报文（I-10 要求报文带名字）。
        text: **实际被嵌入的组合字符串**（逐路径公式见上方白名单注释与各调用点），
            不是裸 description——"长 name/keywords + 短 description"同样能超限。

    Returns:
        None = 放行（未超限，或串太短无需计量）；kind="chunk" 且超限时返回
        分段子块内容列表（调用方负责 source_id / chunk_order_index 归属）。

    Raises:
        EmbedLimitError: description 类超限（I-10 fail-loud），报文含 name 与
            真实 token 数。
    """
    from niu_api.internal import llama_config  # 三阈值唯一真相（函数级导入，与 _count_embed_tokens 的 llama_server 导入同例）
    token_limit = llama_config.embed_token_limit()
    prefilter = llama_config.char_prefilter_adapter()
    if len(text) <= prefilter:
        # ≤ 预筛阈值 ⇒ 最坏 prefilter × 4.000 token < 上限（基线 8000 < 8192），无需计量。
        return None
    tokens = _count_embed_tokens(text)
    if tokens is None:
        # 量不到（llama-server 未就绪等）⇒ 不猜、不吞：放行，由下游 S7 护栏兜底。
        # 此时嵌入同样不可能成功（encode_batch 会先抛"未就绪"），不会产生新的
        # 静默截断窗口；方案 AC-0 的前置条件即依赖 llama-server 就绪。
        logger.warning(
            f"[embed-guard] {kind} 权威计量不可用（>{prefilter} 字符），"
            f"放行由 S7 兜底: name={name!r} chars={len(text)}"
        )
        return None
    if tokens <= token_limit:
        return None
    if kind == "chunk":
        segments = _split_for_embed(text, llama_config.split_char_budget_adapter())
        logger.warning(
            f"[embed-guard] chunk 超限分段: source_id={name!r} tokens={tokens} > "
            f"{token_limit}（{len(text)} 字符）→ {len(segments)} 子块"
        )
        return segments
    raise EmbedLimitError(
        f"{kind} 嵌入内容超限（fail-loud，方案 S8 D-3）：{name!r} 实际嵌入串 "
        f"{tokens} token > 上限 {token_limit}（{len(text)} 字符）。"
        f"拆分单一{kind}在语义上不成立，请调用方自行缩短名称/描述后重试。"
    )


def _count_embed_tokens(text: str) -> int | None:
    """经 llama-server /tokenize 权威计量 token 数（Gemma 口径）。

    与嵌入同一个实例、同一个分词器 ⇒ 计量口径即真实嵌入口径。**不能**改用
    lightrag_repair_tokenizer 的 TiktokenTokenizer（gpt-4o-mini）：那是 LightRAG
    分段口径，与 Gemma 嵌入口径偏离可达 48 倍（方案 R-9）。量不到返回 None——不猜。
    """
    try:
        from niu_api.internal import llama_server

        return llama_server._tokenize_count(text)  # noqa: SLF001 —— S7 同源计量入口
    except Exception as e:  # noqa: BLE001 —— 量不到就说量不到，交由调用方照旧放行
        logger.debug(f"[embed-guard] /tokenize 计量失败: {e}")
        return None


# 段落边界（空行）与句子边界标点，split 时保留分隔符本身，保证子块依次拼接可还原原文。
_PARA_SPLIT_RE = re.compile(r"(\n\n)")
_SENT_SPLIT_RE = re.compile(r"([。！？；!?;\n])")


def _split_for_embed(text: str, budget: int) -> list[str]:
    """把超限文本按语义边界切分为均不超过 ``budget`` 字符的子块（调用方传
    llama_config.split_char_budget_adapter() 的现读值；本函数内只复用、不重复调）。

    边界优先级：段落（空行）→ 句子（。！？；.!?; 与换行）→ 按预算硬切。分隔符作为
    独立单元参与打包（不额外插入字符），故所有子块依次拼接可还原原文。
    每个子块 ≤ budget 字符 ⇒ 最坏（基线 1500 × 4.000 = 6000 < 8192，随生效 -c 缩放）无需再次计量。
    """
    units: list[str] = []

    def _hard_split(s: str) -> None:
        for i in range(0, len(s), budget):
            units.append(s[i : i + budget])

    for part in _PARA_SPLIT_RE.split(text):
        if not part:
            continue
        if len(part) <= budget:
            units.append(part)
            continue
        # 单个段落仍超预算 → 按句子边界再切；单句仍超预算 → 硬切
        for piece in _SENT_SPLIT_RE.split(part):
            if not piece:
                continue
            if len(piece) <= budget:
                units.append(piece)
            else:
                _hard_split(piece)

    segments: list[str] = []
    buf = ""
    for unit in units:
        if buf and len(buf) + len(unit) > budget:
            segments.append(buf)
            buf = unit
        else:
            buf += unit
    if buf:
        segments.append(buf)
    return segments


def _check_entity_name_length(name: str) -> None:
    """D-3.1 快速失败层：实体名长度上界，超界直接 raise（不进入 LightRAG）。"""
    if name and len(name) > _ENTITY_NAME_MAX_CHARS:
        raise EmbedLimitError(
            f"实体名长度超限（fail-loud，方案 S8 D-3.1 快速失败层）："
            f"{name[:64]!r}…（共 {len(name)} 字符 > 上界 {_ENTITY_NAME_MAX_CHARS}）。"
            f"实体名无正当理由长到几百字符，且它会原样进入嵌入串并被改名级联复制到"
            f"每条关联关系。"
        )


def _graph_for_guard(rag):
    """取只读 NetworkX 图句柄（lightrag_manager 同款取法；不复制全图，O(度数)
    读取在 graph_read_lock 内进行）。拿不到返回 None。"""
    graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
    if graph_obj is None:
        return None
    nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
    return nx_graph


def _has_node_for_guard(nx_graph, node_id: str) -> bool:
    """节点存在性（node_id 须已小写化；NetworkXStorage 以 lower 归一化节点 ID）。"""
    if nx_graph is None:
        return False
    try:
        with graph_read_lock():
            return bool(nx_graph.has_node(node_id))
    except Exception as e:  # noqa: BLE001 —— 判不到就按不存在，交由下游照常报错
        logger.debug(f"[embed-guard] has_node 失败: {e}")
        return False


def _node_embed_edges(nx_graph, node_id: str) -> list[tuple[str, str, dict]]:
    """返回 node_id 的全部关联边 (src, tgt, edge_data)（只读，O(度数)。

    供守卫枚举级联重嵌串（D-3.1 主层）。节点不存在/图不可用返回 []。
    """
    if nx_graph is None:
        return []
    try:
        with graph_read_lock():
            if not nx_graph.has_node(node_id):
                return []
            return [
                (u, v, dict(nx_graph.get_edge_data(u, v) or {}))
                for u, v in nx_graph.edges(node_id)
            ]
    except Exception as e:  # noqa: BLE001 —— 读不到边只降级跳过，编辑本身随后会失败
        logger.warning(f"[embed-guard] 关联边读取失败，跳过级联计量: {e}")
        return []


def _guard_edit_entity_entry(
    rag,
    entity_name: str,
    updated_data: dict,
    allow_rename: bool,
    allow_merge: bool,
) -> None:
    """edit_entity 入口守卫（D-3/D-3.1）。必须在方法外层 try **之前**调用：
    该方法末尾有 `except Exception → {"status": "error"}` 兜底，raise 落在 try 内
    会被吞掉，I-10 不成立。

    覆盖（逐路径公式与 utils_graph.py 实际拼接一一对应）：
    - 非改名：第 401 行嵌入 `entity_name + "\\n" + description`；
    - 改名分支直接输入：`new_entity_name + "\\n" + description`；
    - 改名分支级联：每条关联关系重嵌
      `normalized_src + "\\t" + normalized_tgt + "\\n" + keywords + "\\n" + description`
      （src/tgt 含新名，keywords/description 取存储侧，utils_graph.py:374）；
    - 合流分支（新名已存在 + allow_merge=True，utils_graph.py:645-668）：
      non_name_updates 先以**旧名**走一遍 _edit_entity_impl 并嵌入
      `旧名 + "\\n" + description`，之后才 merge 删源；merge 再以
      `keywords + "\\t" + normalized_src + "\\n" + normalized_tgt + "\\n" + description`
      重嵌源（旧）与目标（新）两侧实体的每条关系（utils_graph.py:1474）。
    """
    _check_entity_name_length(entity_name)
    new_name = updated_data.get("entity_name")
    description = str(updated_data.get("description") or "")
    is_rename = isinstance(new_name, str) and bool(new_name) and new_name != entity_name
    if not is_rename:
        _guard_embed_text("entity", entity_name, f"{entity_name}\n{description}")
        return

    _check_entity_name_length(new_name)
    # 改名分支直接输入（Embedding 串 = 新名 + 描述）
    _guard_embed_text("entity", new_name, f"{new_name}\n{description}")
    if not allow_rename:
        # aedit_entity 在任何写入前即 raise（未允许改名），无嵌入发生，
        # 级联串不可能被生成，无需继续计量。
        return

    nx_graph = _graph_for_guard(rag)
    if nx_graph is None:
        logger.warning(
            f"[embed-guard] 图不可用，edit_entity 级联重嵌计量跳过: {entity_name!r}"
        )
        return
    old_key = entity_name.lower()
    new_key = new_name.lower()

    if _has_node_for_guard(nx_graph, new_key):
        if not allow_merge:
            # aedit_entity 在任何写入前即 raise（新名已存在且不允许 merge），
            # 无嵌入发生，无需继续计量。
            return
        # 合流分支：先以旧名嵌入（v1.9 双审 B-P2：只量新名会漏掉这一串）
        _guard_embed_text("entity", entity_name, f"{entity_name}\n{description}")
        # merge 重嵌源（旧）与目标（新）两侧实体的每条关系
        seen: set[tuple[str, str]] = set()
        for node in (old_key, new_key):
            for src, tgt, data in _node_embed_edges(nx_graph, node):
                new_src = new_name if src.lower() == old_key else src
                new_tgt = new_name if tgt.lower() == old_key else tgt
                if new_src == new_tgt:
                    continue  # merge 跳过自环，不重嵌
                nsrc, ntgt = sorted((new_src, new_tgt))
                if (nsrc, ntgt) in seen:
                    continue  # 归一化同 key 的去重路径只 upsert 一次（拼接残留见 R-8）
                seen.add((nsrc, ntgt))
                content = (
                    f"{data.get('keywords') or ''}\t{nsrc}\n{ntgt}\n"
                    f"{data.get('description') or ''}"
                )
                _guard_embed_text("relation", f"{nsrc}|{ntgt}", content)
        return

    # 纯改名分支：对被改名实体的每条关联关系逐条计量级联重嵌串
    for src, tgt, data in _node_embed_edges(nx_graph, old_key):
        new_src = new_name if src.lower() == old_key else src
        new_tgt = new_name if tgt.lower() == old_key else tgt
        nsrc, ntgt = sorted((new_src, new_tgt))
        content = (
            f"{nsrc}\t{ntgt}\n{data.get('keywords') or ''}\n"
            f"{data.get('description') or ''}"
        )
        _guard_embed_text("relation", f"{nsrc}|{ntgt}", content)


def _guard_merge_entities_entry(
    resolved_sources: list[str],
    resolved_target: str,
    target_entity_data: dict | None,
    nx_graph,
) -> None:
    """merge_entities 入口守卫（D-3）。必须在方法外层 try 之前调用。

    覆盖（公式与 _merge_entities_impl 实际拼接一一对应）：
    - 实体侧：`target_entity + "\\n" + description`（description 取自
      target_entity_data 输入；merge_strategy=concatenate 的累加**结果**在
      LightRAG 内部生成、不加钩子，残留见 R-8）；
    - 关系侧重嵌：源与目标两侧实体的每条关系各重嵌一次
      `keywords + "\\t" + normalized_src + "\\n" + normalized_tgt + "\\n" + description`
      （utils_graph.py:1474；关系描述拼接只在归一化同 key 的去重路径发生）。
    """
    target_desc = str((target_entity_data or {}).get("description") or "")
    _guard_embed_text("entity", resolved_target, f"{resolved_target}\n{target_desc}")

    source_keys = {s.lower() for s in resolved_sources}
    seen: set[tuple[str, str]] = set()
    for node in [*resolved_sources, resolved_target]:
        for src, tgt, data in _node_embed_edges(nx_graph, node.lower()):
            new_src = resolved_target if src.lower() in source_keys else src
            new_tgt = resolved_target if tgt.lower() in source_keys else tgt
            if new_src == new_tgt:
                continue  # merge 跳过自环，不重嵌
            nsrc, ntgt = sorted((new_src, new_tgt))
            if (nsrc, ntgt) in seen:
                continue  # 去重路径只 upsert 一次（拼接残留见 R-8）
            seen.add((nsrc, ntgt))
            content = (
                f"{data.get('keywords') or ''}\t{nsrc}\n{ntgt}\n"
                f"{data.get('description') or ''}"
            )
            _guard_embed_text("relation", f"{nsrc}|{ntgt}", content)


def _guard_inject_custom_kg_inputs(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    source_id: str,
) -> list[dict[str, Any]]:
    """inject_custom_kg 入口守卫（D-3）。返回替换用的 chunks 列表。

    - 实体侧逐条计量 `entity_name + "\\n" + description`（lightrag.py:2564 实际
      嵌入串），关系侧逐条计量
      `keywords + "\\t" + src_id + "\\n" + tgt_id + "\\n" + description`
      （lightrag.py:2575）——均 description 类，超限 fail-loud；
    - chunk 正文超限走分段（chunk 语义合法）：返回新列表，**不就地修改调用方
      传入的 chunks 列表**（I-6）。首个子块保留调用方原 source_id（不加后缀，
      I-11）与原 chunk_order_index，其余子块带唯一后缀并按原文顺序递增（I-5）。
    """
    for entity in entities:
        name = entity.get("entity_name") or entity.get("name", "Other")
        desc = str(entity.get("description") or "")
        _guard_embed_text("entity", str(name), f"{name}\n{desc}")
    for rel in relationships:
        keywords = str(rel.get("keywords") or rel.get("relation") or "")
        src = str(rel.get("src_id") or "")
        tgt = str(rel.get("tgt_id") or "")
        desc = str(rel.get("description") or "")
        _guard_embed_text("relation", f"{src}->{tgt}", f"{keywords}\t{src}\n{tgt}\n{desc}")

    guarded: list[dict[str, Any]] = []
    for chunk in chunks:
        content = str(chunk.get("content") or "")
        base_sid = chunk.get("source_id", source_id)
        order = chunk.get("chunk_order_index", 0)
        segments = _guard_embed_text("chunk", str(base_sid), content)
        if segments is None:
            guarded.append(chunk)
            continue
        for idx, seg in enumerate(segments):
            guarded.append({
                "content": seg,
                # I-11：首块保留原 source_id（调用方引用它的关系不会退化为 UNKNOWN），
                # 其余子块带唯一后缀（I-5：不得共用 source_id 撞 chunk_to_source_map）。
                # 后缀带原 order，避免同 source_id 的不同 chunk 分段后互相撞名。
                "source_id": base_sid if idx == 0 else f"{base_sid}_part{order}_{idx}",
                "file_path": chunk.get("file_path", "custom_kg"),
                "chunk_order_index": order + idx,
            })
    return guarded


def _sanitize_graph_error(text: str) -> str:
    """E3 专用脱敏：剥离绝对路径 + API key/Bearer 凭证。

    错误文本可能经 MCP 透传回 LLM（query_data/explore_node/get_graph_snapshot 的
    error message），必须脱敏：绝对路径（/Users/...、~/...、C:\\...）暴露本机目录结构，
    key/Bearer 暴露凭证。与 E2 的 _sanitize_error_msg（litellm_adapter L123-131）同源
    逻辑内联实现——不模块级 import litellm_adapter（避免拉入 runner 依赖链进
    lightrag-server 进程——防循环依赖）。
    """
    # 脱敏 API key（key=xxx, api_key=xxx, apikey=xxx）——复用 E2 _sanitize_error_msg 规则
    text = re.sub(r'(key|api_key|apikey)\s*[=:]\s*\S+', r'\1=***', text, flags=re.IGNORECASE)
    # 脱敏 Bearer token
    text = re.sub(r'Bearer\s+\S+', 'Bearer ***', text, flags=re.IGNORECASE)
    # 剥离绝对路径（/Users/...、~/...、C:\...）
    text = re.sub(r'(?:/Users|~/|C:\\)[^\s,;]+', '***', text)
    return text


def _filter_result_fields(result: dict, fields: list) -> dict:
    """对 query_data 返回结果做字段裁剪，只保留指定字段。

    Args:
        result: query_data 返回的完整结果 dict
        fields: 要保留的字段名列表。None 或空列表表示不过滤。

    Returns:
        裁剪后的结果 dict（原地修改 result 中的 data 部分）
    """
    if not fields:
        return result
    field_set = set(fields)
    data = result.get("data", {})
    if not isinstance(data, dict):
        return result  # Unexpected structure, skip filtering
    # 裁剪 entities
    if "entities" in data:
        data["entities"] = [
            {k: v for k, v in ent.items() if k in field_set}
            for ent in data["entities"]
        ]
    # 裁剪 relationships
    if "relationships" in data:
        data["relationships"] = [
            {k: v for k, v in rel.items() if k in field_set}
            for rel in data["relationships"]
        ]
    # 裁剪 chunks
    if "chunks" in data:
        data["chunks"] = [
            {k: v for k, v in ch.items() if k in field_set}
            for ch in data["chunks"]
        ]
    return result


def _clean_sep(desc: str | None) -> str:
    """Clean LightRAG <SEP> separator from entity/edge descriptions.

    LightRAG merges multi-source descriptions using <SEP> as separator.
    This replaces <SEP> with a space for clean display in API responses
    and MCP tool results returned to the LLM.

    Args:
        desc: Raw description string that may contain <SEP>.

    Returns:
        Description with all <SEP> replaced by spaces. None → empty string.
    """
    if not desc:
        return ""
    return desc.replace("<SEP>", " ")


def _clean_description(desc: str | None, entity_type: str | None = None) -> str:
    """Clean description for output, with brainregion-aware formatting.

    For brainregion entities, the raw description contains brain_meta_*
    metadata separated by <SEP>. This function parses and formats the
    human-readable summary (same logic as kg_api._format_description),
    so both API and MCP consumers receive clean descriptions.

    For all other entity types (and edges), <SEP> is replaced with spaces.

    Args:
        desc: Raw description string that may contain <SEP>.
        entity_type: Entity type string. If "brainregion", applies
            brain_meta parsing before returning.

    Returns:
        Cleaned description string. None → empty string.
    """
    if not desc:
        return ""
    if entity_type and entity_type.lower() == "brainregion" and "<SEP>" in desc:
        from niu_api.internal.region_manager import _format_summary_for_display, _parse_description
        parsed = _parse_description(desc)
        return _format_summary_for_display(parsed)
    return desc.replace("<SEP>", " ")


def _strip_brain_meta(desc: str | None) -> str:
    """从脑区 description 中剔除 brain_meta_* 记账段，只留 summary 部分。

    脑区实体 description 里含每次同步都变的 brain_meta_* 字段（size /
    representative / updated_at，见 region_manager._encode_description）。
    虚拟 chunk 的编号是内容哈希（上游 ainsert_custom_kg 的
    compute_mdhash_id(chunk_content)），且写入是 upsert、从不删除旧编号——
    description 一变，编号漂移、旧编号永不回收，chunk 条数只增不减
    （实测每次启动 +7 条脑区记账块）。把投影进虚拟 chunk 的文本收敛为
    人读 summary 后内容跨同步不变 ⇒ 编号稳定 ⇒ upsert 覆盖同一编号，
    泄漏停止。实体图节点 description 原样保留（brain_meta_* 的下游消费方
    ——priority 衰减 / shrink_count 解散 / size 面板——全部读图节点，
    不读 chunk）。

    切分/拼接口径与 region_manager._parse_description 完全一致：按
    <SEP> 与 | 切分，丢弃 brain_meta_<key>: 段，其余段按 <SEP> 拼回。
    description 不含 brain_meta_* 的实体（人物/照片/技能等）原样返回，
    逐字节 no-op。
    """
    if not desc or "brain_meta_" not in desc:
        return desc if desc else ""
    parts = re.split(r"<SEP>|\s\|\s", desc)
    summary_parts: list[str] = []
    for part in parts:
        part = part.strip()
        if not re.match(r"brain_meta_(\w+):(.*)", part):
            summary_parts.append(part)
    return "<SEP>".join(summary_parts)


def _clean_sep_in_query_result(result):
    """Clean <SEP> from description fields in query_data results.

    LightRAG's aquery_data() returns structured results with entities,
    relationships, and chunks — each potentially containing description
    fields with <SEP> separators from multi-source merging.
    """
    if not isinstance(result, dict):
        return result
    data = result.get("data", result)
    if isinstance(data, dict):
        for key in ("entities", "relationships"):
            items = data.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "description" in item:
                        item["description"] = _clean_sep(item.get("description", ""))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "description" in item:
                item["description"] = _clean_sep(item.get("description", ""))
    return result


class LightRAGAdapter:
    """Query interface for LightRAG.

    Replaces vector-store search and kg-server query with unified
    LightRAG retrieval supporting multiple modes.
    """

# 历史遗留——保留以搜索存量（与 lightrag_manager.custom_entity_types 不再一致：引导类型已去 "tool"，存量 tool 实体仍可搜索/过滤）
    ENTITY_TYPES = {
        "Person", "Organization", "Technology", "Concept",
        "Location", "Event", "Document", "Photo", "Video",
        "Note", "Chat", "Skill", "Tool", "Knowledge",
        "EpisodicEvent", "brainregion", "other",
    }

    @staticmethod
    def _is_no_result(result: dict[str, Any] | None) -> bool:
        """Check whether a LightRAG query_data result is effectively empty.

        Detects all documented "no result" scenarios from aquery_data:
        - None (adapter caught an exception)
        - {} empty dict (tokenizer missing, empty keywords, etc.)
        - {"status": "failure", ...} (KG search found nothing)
        - {"status": "success", "data": {"entities": [], ...}} (bypass mode,
          all inner lists empty)

        E3 契约（D3 A4 P3-5 实证——现状已排除，无新增逻辑）：
        error dict（{"status": "error", "message": ...}——E3 分类后的错误形态）
        不在上述场景内——status != "failure" 且无 data 键 → 走到底部 return False，
        **不归入 no_results 真空判定**（错误不再伪装为无结果）。

        Prioritises structural fields (status, data content) over string
        matching so the check is robust against LightRAG version changes.

        Args:
            result: Return value from query_data().

        Returns:
            True if the result contains no usable data.
        """
        if result is None:
            return True

        if not isinstance(result, dict):
            return True

        if not result:
            # Empty dict — tokenizer missing, empty keywords, etc.
            return True

        # Structural check: explicit failure status from LightRAG
        status = result.get("status")
        if status == "failure":
            return True

        # Check data payload: if all list-valued fields are empty, treat as
        # no result (covers bypass mode with empty entities/relationships/chunks)
        data = result.get("data")
        if isinstance(data, dict):
            if data:
                # Data dict exists but all list values are empty
                list_values = [v for v in data.values() if isinstance(v, list)]
                if list_values and all(not v for v in list_values):
                    return True
            else:
                # Empty data dict with success status — still no results
                return True

        return False

    @staticmethod
    def _is_error_text(text: str | None) -> bool:
        """Check whether a text result is a LightRAG canned error message.

        Detects the fail_response constant that LightRAG returns via the
        aquery() string path:
            "Sorry, I'm not able to provide an answer to that question.[no-context]"

        This is NOT an LLM-generated response — it is a hard-coded fallback
        that must never leak into system prompts.

        Args:
            text: A string query result from query() / aquery().

        Returns:
            True if the text is a LightRAG error/fail response.
        """
        if not text or not isinstance(text, str):
            return False
        lower = text.lower()
        return any(marker in lower for marker in _LIGHTRAG_ERROR_MARKERS)

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    def query(
        self,
        query: str,
        mode: str = "mix",
        only_need_context: bool = True,
        top_k: int | None = None,
        response_type: str | None = None,
        keywords: list[str] | None = None,
        timeout: int = 120,
    ) -> str | None:
        """Query the brain graph / knowledge base.

        Args:
            query: The search query string.
            mode: Retrieval mode (naive, local, global, hybrid, mix, bypass).
            only_need_context: If True, return context without LLM generation.
            top_k: Number of top items to retrieve.
            response_type: Response format (e.g., "Bullet Points").
            keywords: Pre-provided keywords to skip LLM extraction.
                When provided, both hl_keywords and ll_keywords are set,
                avoiding LLM calls entirely (near-instant return).
            timeout: Maximum seconds to wait for the LightRAG query result.
                Default is 120. Callers can set a shorter timeout (e.g. 2)
                to prevent deadlock when the LightRAG event loop is busy.

        Returns:
            Query result string, or error text string on failure.
            fail_response ("" ) keeps its vacuum no-results semantics (never leaks
            into system prompts); real exceptions return "[图谱查询失败: <err>]";
            gate-rejected LightRAG (rag None) returns the generic unavailable text.

        Raises:
            ValueError: If mode is invalid.
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {sorted(VALID_MODES)}"
            )

        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, query failed")
            # E3 契约反转：错误不再伪装为无结果——门控拒绝返回错误文本 str（通用文案）
            return _GRAPH_UNAVAILABLE_MSG

        try:
            from lightrag import QueryParam

            param = QueryParam(mode=mode, only_need_context=only_need_context)
            # 无 rerank 模型（reranker_model="none" 未注入 rerank_model_func），显式关闭
            # 避免每次查询走空 rerank 分支 + 警告（LightRAG fork utils.py apply_rerank_if_enabled）；
            # 若日后配置 rerank 模型，此开关需重新评估
            param.enable_rerank = False
            if keywords is not None:
                param.hl_keywords = keywords
                param.ll_keywords = keywords
            if top_k is not None:
                param.top_k = top_k
            if response_type is not None:
                param.response_type = response_type

            result = call_async(rag.aquery(query, param=param), timeout=timeout)
            # aquery() type is str | AsyncIterator[str]; with only_need_context=True
            # it always returns str. Guard against non-string just in case.
            if not isinstance(result, str):
                return None
            if self._is_error_text(result):
                logger.debug("LightRAG query() returned fail_response, filtering out")
                return ""
            # Clean <SEP> from context string (entity descriptions embedded by LightRAG)
            result = result.replace("<SEP>", " ")
            return result

        except Exception as e:
            logger.error(f"LightRAG query failed: {e}")
            # E3 契约反转：错误不再伪装为无结果——真异常返回错误文本 str
            # （[图谱查询失败: <err>]——MCP 工具层据此转 error dict）
            err_msg = _sanitize_graph_error(str(e) or f"{type(e).__name__}: (no message)")
            return f"[图谱查询失败: {err_msg}]"

    def query_data(
        self,
        query: str,
        mode: str = "local",
        top_k: int | None = None,
        keywords: list[str] | None = None,
        filter_lambda=None,
        fields: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any] | None:
        """Query LightRAG returning structured data (entities + relationships).

        Uses aquery_data() instead of aquery() to get structured results with
        entity_type information, which is needed for type-based filtering.

        When keywords are provided, skips LLM keyword extraction for near-instant
        results while keeping full graph traversal. Without keywords, LLM extraction
        adds 5-30s latency.

        Args:
            query: The search query string.
            mode: Retrieval mode (default "local" for entity-focused).
            top_k: Number of top items to retrieve.
            keywords: Pre-provided search keywords to skip LLM extraction.
                For "local" mode: used as ll_keywords (entity search).
                For "global"/"hybrid"/"mix": used as both hl and ll keywords.
            filter_lambda: Optional filter function for LightRAG query.
            fields: Optional list of field names to include in the output.
                When provided, only these fields are kept in each entity/relationship/chunk.
                Common choices: ["entity_name", "entity_type"] for name-only lists.
                None (default) returns all fields (no filtering).
            timeout: Timeout seconds for the underlying call_async; None uses the
                configured lightrag_timeout("query_timeout", 120) default.

        Returns:
            Structured query result dict, or error dict
            {"status": "error", "message": <err>} on failure / gate rejection.

        Raises:
            ValueError: If mode is invalid.
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {sorted(VALID_MODES)}"
            )

        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, query_data failed")
            # E3 契约反转：错误不再伪装为无结果——门控拒绝返回 error dict（通用文案）
            return {"status": "error", "message": _GRAPH_UNAVAILABLE_MSG}

        try:
            from lightrag import QueryParam

            param = QueryParam(mode=mode)
            # 无 rerank 模型（reranker_model="none" 未注入 rerank_model_func），显式关闭
            # 避免每次查询走空 rerank 分支 + 警告（LightRAG fork utils.py apply_rerank_if_enabled）；
            # 若日后配置 rerank 模型，此开关需重新评估
            param.enable_rerank = False
            if top_k is not None:
                param.top_k = top_k
            if keywords:
                param.ll_keywords = keywords
                if mode in ("global", "hybrid", "mix"):
                    param.hl_keywords = keywords
            if filter_lambda is not None:
                param.filter_lambda = filter_lambda

            result = call_async(
                rag.aquery_data(query, param=param),
                timeout=timeout if timeout is not None else lightrag_timeout("query_timeout", 120),
            )
            if fields:
                result = _filter_result_fields(result, fields)
            result = _clean_sep_in_query_result(result)

            # 精确名短路：query 恰为实体名时，图层精确索引命中实体置顶
            # （向量排序不决定精确名查询命运——高频实体向量 rank 掉出修复；不动 LightRAG fork）
            try:
                data = result.get("data", result)
                if (
                    result.get("status") == "success"  # G1: failure 形态（data 为空）不短路
                    and filter_lambda is None  # G2: filter_lambda 技能通道契约不绕过
                    and mode in ("local", "global", "hybrid", "mix")  # G3: naive/bypass 实体数组契约为空
                    and isinstance(data, dict)  # G4: entities 双形态——data 为 list 时跳过
                    and isinstance(data.get("entities"), list)
                ):
                    q = query.strip()
                    # ≤50 字符门控：组合长句 query 不命中自动跳过（罕见长实体名接受不短路）
                    if q and len(q) <= 50 and self.has_entity(q):
                        info = self.get_entity_info(q)
                        if info.get("status") == "ok":
                            gd = info["data"]["graph_data"]
                            # 无 rank 字段（rank 是 operate.py 内部中间值）；distance 不可得——
                            # 缺失时 runner 衰减池 fallback 1.0=池内最高分，恰符合置顶意图
                            hit = {"entity_name": q}
                            for f in ("entity_type", "description", "source_id", "file_path", "created_at"):
                                v = gd.get(f)
                                if v is not None:
                                    hit[f] = v
                            if fields:
                                field_set = set(fields)
                                hit = {k: v for k, v in hit.items() if k in field_set}
                            entities = data["entities"]
                            idx = next(
                                (i for i, e in enumerate(entities)
                                 if isinstance(e, dict)
                                 and str(e.get("entity_name", "")).lower() == q.lower()),
                                None,
                            )
                            if idx is not None:
                                entities.insert(0, entities.pop(idx))
                            else:
                                orig_len = len(entities)
                                entities.insert(0, hit)
                                limit = top_k if top_k is not None else orig_len
                                del entities[limit:]
                            logger.debug(f"query_data 精确名短路: '{q}' 图层索引命中，置顶")
            except Exception as e:
                logger.warning(f"query_data 精确名短路异常（返回原向量结果）: {e}")
            return result

        except Exception as e:
            logger.error(f"LightRAG query_data failed: {e}")
            # E3 契约反转：错误不再伪装为无结果——真异常返回 error dict（带脱敏错误文本）
            err_msg = _sanitize_graph_error(str(e) or f"{type(e).__name__}: (no message)")
            return {"status": "error", "message": err_msg}

    def filter_by_entity_type(
        self,
        query_result: dict[str, Any] | None,
        entity_type: str,
    ) -> list[dict[str, Any]]:
        """Filter LightRAG query results by entity_type.

        LightRAG's aquery_data() returns structured results containing entities
        with entity_type fields. This method extracts only entities matching
        the requested type.

        Args:
            query_result: Structured result from query_data(), or None.
            entity_type: Entity type to filter for (e.g., "skill", "tool").

        Returns:
            List of entity dicts matching the requested type.
        """
        if query_result is None:
            return []

        # aquery_data returns {status, message, data: {entities, relationships, chunks}}
        data = query_result.get("data", {})
        if not data:
            # Fallback: query_result might be the data dict directly
            data = query_result

        entities = data.get("entities", [])
        if not entities:
            return []

        # Normalize entity_type for case-insensitive comparison
        target_type = entity_type.lower().strip()
        filtered = []
        for entity in entities:
            et = entity.get("entity_type", "")
            if et and et.lower().strip() == target_type:
                filtered.append(entity)

        return filtered

    # ============== Multi-Category Search ==============

    # Mapping from LightRAG entity_type to the category keys used by
    # _inject_dynamic_resources() in runner.py.
    # Keys use lowercase for case-insensitive matching (.lower() applied at lookup).
    _ENTITY_TYPE_TO_CATEGORY = {
        "skill": "skill",
        "tool": "knowledge",
        "knowledge": "knowledge",
        "concept": "knowledge",
        "person": "knowledge",
        "photo": "knowledge",
        "organization": "knowledge",
        "technology": "knowledge",
        "location": "knowledge",
        "event": "knowledge",
        "document": "knowledge",
        "video": "knowledge",
        "note": "knowledge",
        "chat": "knowledge",
        "episodicevent": "knowledge",
        "brainregion": "knowledge",
        "other": "other",
    }

    def _categorize_results(self, result: dict) -> dict[str, list[dict]]:
        """Categorize query_data results into skill/knowledge/other buckets by entity_type.

        Includes same fallback logic as search_multi_lightrag for data extraction.
        """
        buckets: dict[str, list[dict]] = {cat: [] for cat in set(self._ENTITY_TYPE_TO_CATEGORY.values())}

        if not result:
            return buckets

        data = result.get("data", {})
        if not data:
            data = result
        entities = data.get("entities", [])
        if not entities:
            return buckets

        for entity in entities:
            entity_type = entity.get("entity_type", "other").lower()
            category = self._ENTITY_TYPE_TO_CATEGORY.get(entity_type, "knowledge")
            buckets[category].append(entity)
        return buckets

    def search_multi_lightrag(
        self,
        query: str,
        mode: str = "local",
        top_k: int = 20,
        keywords: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Single-query multi-category search via LightRAG.

        Performs one query_data() call and groups entities by entity_type
        into the category buckets used by _inject_dynamic_resources().

        This is the primary retrieval method for dynamic resource injection,
        replacing vector_search.search_multi() as the main path.

        When keywords are provided, skips LLM keyword extraction for near-instant
        results while keeping full graph traversal. Without keywords, LLM extraction
        adds 5-30s latency.

        Args:
            query: Search query string.
            mode: LightRAG retrieval mode (default "local" for entity-focused).
            top_k: Total number of entities to retrieve.
            keywords: Pre-provided search keywords to skip LLM extraction.
                For "local" mode: used as ll_keywords (entity search).
                For "global"/"hybrid"/"mix": used as both hl and ll keywords.
            timeout: Timeout seconds, passed through to query_data().

        Returns:
            Dict with category keys matching _ENTITY_TYPE_TO_CATEGORY values,
            each mapping to a list of entity dicts. Empty lists on error or no results.
        """
        # Initialize result dict with all category buckets from the mapping
        result: dict[str, list[dict[str, Any]]] = {
            cat: [] for cat in self._ENTITY_TYPE_TO_CATEGORY.values()
        }

        query_result = self.query_data(
            query, mode=mode, top_k=top_k, keywords=keywords,
            timeout=timeout,
        )
        if isinstance(query_result, dict) and query_result.get("status") == "error":
            # E3 契约反转：错误不再伪装为无结果——raise 传导至 runner 既有 except
            # （不 raise 则 _categorize_results 会把 error dict 当数据取空桶——错误静默丢失）
            raise RuntimeError(query_result.get("message") or _GRAPH_UNAVAILABLE_MSG)
        if self._is_no_result(query_result):
            logger.debug("LightRAG search_multi_lightrag: query_data returned no results")
            return result

        return self._categorize_results(query_result)

    def search_within_region(
        self,
        query: str,
        region_member_names: set[str] | list[str],
        mode: str = "local",
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> dict[str, list[dict]]:
        """Search entities within specified brain region members only.

        Uses filter_lambda to restrict vector search to the given member entity names.
        This enables region-scoped semantic search (e.g., searching only within
        activated brain regions).

        Args:
            query: Search query text
            region_member_names: Set/list of entity names to restrict search to
            mode: LightRAG search mode (default: "local")
            top_k: Number of results to return
            keywords: Optional keywords to skip LLM extraction

        Returns:
            Dict with "skill", "knowledge" and "other" lists, same format as search_multi_lightrag
        """
        if not region_member_names:
            return {"skill": [], "knowledge": [], "other": []}

        member_set = {name.lower() for name in region_member_names}
        def filter_fn(data):
            return (data.get("entity_name") or "").lower() in member_set

        result = self.query_data(
            query, mode=mode, top_k=top_k, keywords=keywords,
            filter_lambda=filter_fn,
        )
        if not result:
            return {"skill": [], "knowledge": [], "other": []}

        return self._categorize_results(result)

    # ============== Semantic Search Methods ==============

    def search_skills(
        self,
        query: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for skill entities in the knowledge graph.

        Uses local mode (entity-focused) and filters by entity_type="skill".

        Args:
            query: Search query string.
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of skill entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        return self.filter_by_entity_type(result, "Skill")

    def search_by_file_path(
        self,
        query: str,
        file_path_contains: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search entities with pre-filter on file_path via filter_lambda.

        Unlike search_skills (which post-filters), this method uses
        filter_lambda to filter at the vector search stage — achieving
        true 'filter-then-top-k' semantics.

        Used for skill retrieval where file_path contains 'skill_sync'
        (SkillSync-injected skills), ensuring skills are not drowned out
        by knowledge entities in global top-k.

        Args:
            query: Search query string.
            file_path_contains: Substring to match in entity's file_path field.
            top_k: Number of top results to retrieve (after filtering).
            keywords: Pre-provided keywords to skip LLM extraction.
            timeout: Timeout seconds, passed through to query_data().

        Returns:
            List of entity dicts matching the file_path filter.
        """
        def filter_fn(data: dict) -> bool:
            fp = data.get("file_path", "")
            return bool(fp) and file_path_contains in fp

        result = self.query_data(
            query, mode="local", top_k=top_k,
            keywords=keywords, filter_lambda=filter_fn,
            timeout=timeout,
        )
        if isinstance(result, dict) and result.get("status") == "error":
            # E3 契约反转：错误不再伪装为无结果——raise 传导至 runner 既有 except
            raise RuntimeError(result.get("message") or _GRAPH_UNAVAILABLE_MSG)
        if not result:
            return []

        data = result.get("data", {})
        if not data:
            data = result
        return data.get("entities", [])

    def search_tools(
        self,
        query: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for tool entities in the knowledge graph.

        Uses local mode (entity-focused) and filters by entity_type="tool".

        Args:
            query: Search query string.
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of tool entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        return self.filter_by_entity_type(result, "Tool")

    def search_knowledge(
        self,
        query: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for knowledge and concept entities in the knowledge graph.

        Uses local mode (entity-focused) and filters by entity_type="knowledge"
        or entity_type="concept" (both are semantic knowledge).

        Args:
            query: Search query string.
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of knowledge/concept entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        knowledge_entities = self.filter_by_entity_type(result, "Knowledge")
        concept_entities = self.filter_by_entity_type(result, "Concept")
        return knowledge_entities + concept_entities

    # ============== Graph Traversal Methods ==============

    # DEPRECATED: 截断已移至 agent_loop 统一关口，本函数保留供参考但无调用方
    def _truncate_graph_result(self, result: dict[str, Any], tool_name: str = "lightrag_get_graph") -> dict[str, Any]:
        """图查询结果保底截断到 LIGHTRAG_GRAPH_MAX_CHARS。

        explore_node 和 get_graph_snapshot 共用。用 while 循环逐步缩减 nodes
        直到序列化 <= 上限，避免按比例截断后仍超限。

        截断策略：
        - 清空 edges（占字符最多且可重新查询）
        - 保留 center + 部分 nodes 让 LLM 知道查询方向
        - stats 保留原始计数 + kept_nodes 让 LLM 知道截断比例
        """
        import json
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized) <= LIGHTRAG_GRAPH_MAX_CHARS:
            return result

        logger.warning(f"{tool_name} result {len(serialized)} chars > {LIGHTRAG_GRAPH_MAX_CHARS}, truncating")
        center = result.get("center")
        nodes = list(result.get("nodes", []))
        edges = list(result.get("edges", []))
        original_nodes = len(nodes)
        original_edges = len(edges)
        stats_extra = result.get("stats", {})

        # 先处理 center 超大边界：若 center 自身 description 过大（如 nodes=[] 时
        # while 循环 keep_count=0 立即退出但仍返回 60K），提前截断 description
        if center is not None and isinstance(center, dict):
            try:
                center_serialized = json.dumps(center, ensure_ascii=False)
            except Exception:
                center_serialized = ""
            if len(center_serialized) > LIGHTRAG_GRAPH_MAX_CHARS // 2:
                # center 占了一半以上预算，截断 description 到 1/4 预算
                desc = center.get("description", "")
                if isinstance(desc, str) and len(desc) > 0:
                    desc_budget = LIGHTRAG_GRAPH_MAX_CHARS // 4
                    center = {
                        **center,
                        "description": desc[:desc_budget] + "...[center 截断]",
                    }

        # 先清空 edges（占字符最多且可重新查询），逐步缩减 nodes
        truncated_nodes = list(nodes)
        while True:
            candidate = {
                "status": "truncated",
                "message": f"[截断] {tool_name} 原始输出 {len(serialized)} 字符，已截断至 {LIGHTRAG_GRAPH_MAX_CHARS} 字符。请缩小 depth/limit 参数后重新查询。",
                "center": center,
                "nodes": truncated_nodes,
                "edges": [],
                "stats": {
                    **stats_extra,
                    "nodes": original_nodes,
                    "edges": original_edges,
                    "truncated": True,
                    "original_chars": len(serialized),
                    "kept_nodes": len(truncated_nodes),
                },
            }
            candidate_serialized = json.dumps(candidate, ensure_ascii=False)
            if len(candidate_serialized) <= LIGHTRAG_GRAPH_MAX_CHARS or len(truncated_nodes) == 0:
                return candidate
            keep_count = max(0, int(len(truncated_nodes) * 0.7))
            truncated_nodes = truncated_nodes[:keep_count]

    def explore_node(self, entity_name: str, depth: int = 2, edge_types: list[str] | None = None) -> dict[str, Any]:
        """Get neighbors of an entity in the knowledge graph.

        Uses LightRAG's built-in get_knowledge_graph() method which performs
        BFS from the given node and returns a structured subgraph.

        Returns structured data compatible with the kg-server explore_node
        output format: {center, nodes, edges, stats}.

        Args:
            entity_name: Entity name to explore from.
            depth: BFS traversal depth (1-5, default 2).
            edge_types: Optional filter — only return edges whose relation
                        is in this list. None returns all edges.

        Returns:
            Dict with center, nodes, edges, and stats keys.
            Returns empty shell dict plus {"status": "error", "message": ...}
            on error or gate rejection.
        """
        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, explore_node failed")
            # E3 契约反转：错误不再伪装为无结果——门控拒绝返回空壳 + error dict
            return {
                "center": None, "nodes": [], "edges": [],
                "stats": {"nodes": 0, "edges": 0, "max_depth": depth},
                "status": "error", "message": _GRAPH_UNAVAILABLE_MSG,
            }

        depth = max(1, min(5, depth))

        try:
            # LightRAG's get_knowledge_graph performs BFS from node_label
            # Returns KnowledgeGraph(nodes=[KnowledgeGraphNode], edges=[KnowledgeGraphEdge])
            kg = call_async(
                rag.get_knowledge_graph(entity_name, max_depth=depth),
                timeout=lightrag_timeout("query_timeout", 120),
            )

            if kg is None or (not kg.nodes and not kg.edges):
                # 真空分支（D1/D3/D7）：实体不存在/无邻居是真实无结果场景——
                # 返回无 status 空壳（仅异常与门控拒绝路径带 error dict）
                return {
                    "center": None, "nodes": [], "edges": [],
                    "stats": {"nodes": 0, "edges": 0, "max_depth": depth},
                }

            # Build center from the first node (should be the queried entity)
            center = None
            if kg.nodes:
                first_node = kg.nodes[0]
                center = {
                    "id": first_node.id,
                    "name": first_node.id,
                    "type": first_node.properties.get("entity_type", "other"),
                    "description": _clean_description(first_node.properties.get("description", ""), first_node.properties.get("entity_type", "other")),
                    "file_path": first_node.properties.get("file_path", ""),
                    "source_id": first_node.properties.get("source_id", ""),
                }

            # Convert KnowledgeGraphNode to frontend-compatible format
            nodes = []
            for node in kg.nodes:
                nodes.append({
                    "id": node.id,
                    "name": node.id,
                    "type": node.properties.get("entity_type", "other"),
                    "description": _clean_description(node.properties.get("description", ""), node.properties.get("entity_type", "other")),
                    "file_path": node.properties.get("file_path", ""),
                    "source_id": node.properties.get("source_id", ""),
                })

            # Convert KnowledgeGraphEdge to frontend-compatible format
            edges = []
            for edge in kg.edges:
                edges.append({
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.properties.get("keywords", ""),
                    "description": _clean_sep(edge.properties.get("description", "")),
                    "weight": edge.properties.get("weight", 1.0),
                })

            # Filter edges by edge_types if specified
            if edge_types is not None:
                edge_type_set = set(edge_types)
                edges = [e for e in edges if e.get("relation") in edge_type_set]

            result = {
                "center": center,
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "max_depth": depth,
                },
            }
            # 截断由 agent_loop 统一关口处理（Agent 工具调用路径）
            # 前端 API 和内部业务（region_detector/region_manager）直接调此方法，不被截断
            return result

        except Exception as e:
            logger.error(f"LightRAG explore_node failed: {e}")
            # E3 契约反转：错误不再伪装为无结果——真异常返回空壳 + error dict
            err_msg = _sanitize_graph_error(str(e) or f"{type(e).__name__}: (no message)")
            return {
                "center": None, "nodes": [], "edges": [],
                "stats": {"nodes": 0, "edges": 0, "max_depth": depth},
                "status": "error", "message": err_msg,
            }

    def timeline_query(
        self,
        query: str,
        start_entities: list[str] | None = None,
        direction: str = "backward",
        max_depth: int = 2,
        top_k: int = 5,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """时间线查询：向量匹配内容 → 遍历时间链 → 按时间戳排序。

        Uses query_data() for vector search to find matching entities,
        then explore_node() to traverse time-chain relations from those entities.
        Only edges with timeline relation types are included in results.

        Args:
            query: 查询文本。
            start_entities: 直接指定起始实体名，跳过向量匹配步骤。
            direction: 排序方向 — "backward"（最近优先）或 "forward"（最早优先）。
            max_depth: 时间链遍历深度。
            top_k: 向量搜索返回的实体数。
            max_results: 返回结果最大数量。

        Returns:
            按时间戳排序的时间线结果列表。
        """
        timeline_edge_types = {"followed_by", "corrected_by", "led_to", "resolved_by"}

        # Step 1: Determine starting entities
        if start_entities:
            entity_names = start_entities
        else:
            # Vector search to find matching entities (Agent-initiated, let LLM extract keywords)
            query_result = self.query_data(
                query, mode="local", top_k=top_k,
            )

            if isinstance(query_result, dict) and query_result.get("status") == "error":
                # E3 契约反转：错误不再伪装为无结果——query_data error dict 不再吞错返回 []
                # raise 从 timeline_query 顶层出——MCP lightrag_timeline_query except 捕获转 error dict
                raise RuntimeError(query_result.get("message") or _GRAPH_UNAVAILABLE_MSG)

            if query_result is None:
                return []

            data = query_result.get("data", {}) if isinstance(query_result, dict) else {}
            if not data:
                data = query_result
            entities = data.get("entities", [])
            if not entities:
                return []

            entity_names = [
                e.get("entity_name", "") if isinstance(e, dict) else str(e)
                for e in entities[:top_k]
            ]

        # Step 2: Traverse time-chain relations from matched entities
        timeline_items: list[dict[str, Any]] = []
        seen_entities: set = set()

        for entity_name in entity_names:
            if not entity_name or entity_name in seen_entities:
                continue
            seen_entities.add(entity_name)

            # Add the matched entity itself
            # Try to get entity description from explore_node(depth=0)
            entity_desc = ""
            try:
                node_data = self.explore_node(entity_name, depth=0)
                for node in node_data.get("nodes", []):
                    if node.get("id") == entity_name or node.get("name") == entity_name:
                        entity_desc = _clean_sep(node.get("description", ""))
                        break
            except Exception as e:
                logger.debug(f"timeline_query: explore_node({entity_name}, depth=0) failed: {e}")

            timestamp = self._extract_timestamp(entity_desc)
            timeline_items.append({
                "entity_name": entity_name,
                "description": entity_desc,
                "timestamp": timestamp,
                "relation": "match",
            })

            # Traverse edges via explore_node, keeping only timeline edges
            try:
                node_data = self.explore_node(entity_name, depth=max_depth)
            except Exception as e:
                logger.debug(f"timeline_query: explore_node({entity_name}, depth={max_depth}) failed: {e}")
                continue

            # explore_node returns edges at top level, not nested in nodes
            sub_edges = node_data.get("edges", []) if isinstance(node_data, dict) else []
            for edge in sub_edges:
                relation = edge.get("relation", "")
                if relation not in timeline_edge_types:
                    continue
                edge_desc = _clean_sep(edge.get("description", ""))
                edge_timestamp = self._extract_timestamp(edge_desc)
                target_name = edge.get("target", "")
                if target_name and target_name not in seen_entities:
                    seen_entities.add(target_name)
                    timeline_items.append({
                        "entity_name": target_name,
                        "description": edge_desc,
                        "timestamp": edge_timestamp,
                        "relation": relation,
                    })

        # Step 3: Sort by timestamp based on direction
        reverse_sort = direction == "backward"
        timeline_items.sort(
            key=lambda x: x.get("timestamp") or "",
            reverse=reverse_sort,
        )

        return timeline_items[:max_results]

    @staticmethod
    def _extract_timestamp(description: str) -> str:
        """从描述中提取 created_at 时间戳。

        格式: created_at=2026-04-27T14:00:00|其他内容

        Args:
            description: Entity/edge description string.

        Returns:
            Extracted timestamp string, or empty string if not found.
        """
        if not description:
            return ""
        for part in description.split("|"):
            if part.startswith("created_at="):
                return part[len("created_at="):]
        return ""

    def has_entity(self, entity_name: str) -> bool:
        """精确查询实体是否已存在（LightRAG内部lowercase存储，此处直接lowercase匹配）。

        Returns False when LightRAG is not initialized (lenient — allows insert
        to proceed; ainsert_custom_kg will upsert/overwrite existing nodes).
        """
        rag = self._get_rag()
        if rag is None:
            return False
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return False
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        from niu_api.internal.lightrag_manager import graph_read_lock
        with graph_read_lock():
            return nx_graph.has_node(entity_name.lower())

    def has_edge(self, src_id: str, tgt_id: str, keywords: str = None) -> bool:
        """精确查询关系是否已存在。

        If keywords is provided, also checks that the edge's keywords matches.
        Without keywords, returns True if any edge exists between src and tgt.

        Returns False when LightRAG is not initialized (lenient — allows insert
        to proceed; ainsert_custom_kg will upsert/overwrite existing edges).
        """
        rag = self._get_rag()
        if rag is None:
            return False
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return False
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        from niu_api.internal.lightrag_manager import graph_read_lock
        with graph_read_lock():
            src = src_id.lower()
            tgt = tgt_id.lower()
            if not nx_graph.has_edge(src, tgt):
                return False
            if keywords is None:
                return True
            edge_data = nx_graph.get_edge_data(src, tgt)
            return edge_data.get("keywords", "").lower() == (keywords or "").lower()

    def get_graph_snapshot(self, limit: int = 2000) -> dict[str, Any]:
        """Return all nodes and edges from LightRAG knowledge graph.

        Reads the NetworkX graph directly (same pattern as hub_entities,
        list_entities, etc.) instead of going through call_async. This avoids
        blocking on the LightRAG event loop when background sync operations
        (lightrag_sync, region_sync) are writing via inject_custom_kg.

        Args:
            limit: Maximum number of nodes to return (default 200).
                Nodes are sorted by degree (most-connected first).

        Returns:
            Dict with nodes and edges lists for frontend visualization.
            Returns empty shell dict plus {"status": "error", "message": ...}
            on error or gate rejection.
        """
        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, get_graph_snapshot failed")
            # E3 契约反转：错误不再伪装为无结果——门控拒绝返回空壳 + error dict
            return {"nodes": [], "edges": [], "status": "error", "message": _GRAPH_UNAVAILABLE_MSG}

        try:
            from niu_api.internal.lightrag_manager import graph_read_lock

            graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
            if graph_obj is None:
                return {"nodes": [], "edges": []}
            nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj

            # Acquire read lock to prevent concurrent graph mutation during
            # traversal. Without this, background sync (lightrag_sync, region_sync)
            # can modify the graph via inject_custom_kg while we iterate, causing
            # RuntimeError or silent data corruption.
            with graph_read_lock():
                # Shallow-copy the graph under the lock to get a consistent snapshot.
                # The copy itself is fast (<1ms for ~200 nodes). After copying,
                # we can release the lock and iterate the snapshot safely.
                try:
                    snapshot = nx_graph.copy()
                except Exception as e:
                    logger.warning(f"[KG] graph copy failed: {e}, returning error snapshot")
                    # E3 契约反转：错误不再伪装为无结果——图拷贝失败返回空壳 + error dict
                    err_msg = _sanitize_graph_error(str(e) or f"{type(e).__name__}: (no message)")
                    return {"nodes": [], "edges": [], "status": "error", "message": err_msg}

            # Now iterate the snapshot without holding the lock (safe because
            # it's our local copy — no other thread can modify it)
            # Sort nodes by degree (most-connected first), then take top limit
            # When limit <= 0, return ALL nodes (no truncation)
            if limit <= 0:
                top_nodes = list(snapshot.nodes())
                top_set = set(top_nodes)
            else:
                try:
                    node_degrees = {n: snapshot.degree(n) for n in snapshot.nodes()}
                    sorted_nodes = sorted(
                        node_degrees.keys(), key=lambda n: node_degrees[n], reverse=True
                    )
                    top_nodes = sorted_nodes[:limit]
                    top_set = set(top_nodes)
                except RuntimeError:
                    logger.warning("Graph modified during snapshot read, returning partial data")
                    top_nodes = list(snapshot.nodes())[:limit]
                    top_set = set(top_nodes)

            nodes = []
            for node_name in top_nodes:
                try:
                    attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                except (RuntimeError, KeyError):
                    attrs = {}
                nodes.append({
                    "id": node_name,
                    "name": node_name,
                    "type": attrs.get("entity_type", "other"),
                    "description": _clean_description(attrs.get("description", ""), attrs.get("entity_type", "other")),
                    "file_path": attrs.get("file_path", ""),
                    "source_id": attrs.get("source_id", ""),
                })

            edges = []
            try:
                for u, v, data in snapshot.edges(data=True):
                    if u in top_set and v in top_set:
                        edges.append({
                            "source": u,
                            "target": v,
                            "relation": data.get("keywords", ""),
                            "description": _clean_sep(data.get("description", "")),
                            "weight": data.get("weight", 1.0),
                        })
            except RuntimeError:
                logger.warning("Graph modified during snapshot edge read, returning partial edges")

            result = {
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "limit": limit,
                },
            }
            # 截断由 agent_loop 统一关口处理（Agent 工具调用路径）
            # 前端 API（kg_api.py graph_snapshot 端点）直接调此方法，不被截断
            return result

        except Exception as e:
            logger.error(f"LightRAG get_graph_snapshot failed: {e}")
            # E3 契约反转：错误不再伪装为无结果——真异常返回空壳 + error dict
            err_msg = _sanitize_graph_error(str(e) or f"{type(e).__name__}: (no message)")
            return {"nodes": [], "edges": [], "status": "error", "message": err_msg}

    # ============== Management Methods ==============

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        """Delete a document and all its related entities/relations by doc_id.

        Uses LightRAG's adelete_by_doc_id for cascading deletion: removes the
        document's chunks, entities (if fully owned), and relationships (if
        fully owned).  Partially-owned entities/relationships are rebuilt from
        remaining documents.

        Args:
            doc_id: Document ID (e.g., "note:shopping").

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.adelete_by_doc_id(doc_id), timeout=lightrag_timeout("delete_timeout", 300))

            # Check DeletionResult status
            if hasattr(result, "status"):
                if result.status == "not_found" or (hasattr(result, "status_code") and getattr(result, "status_code", None) == 404):
                    logger.info(f"LightRAG delete_document: doc '{doc_id}' not found, treated as ok")
                    return {"status": "ok", "doc_id": doc_id, "note": "not_found_treated_as_ok"}
                elif result.status in ("not_allowed", "fail"):
                    logger.warning(f"LightRAG delete_document failed for '{doc_id}': status={result.status}")
                    return {"status": "error", "message": f"DeletionResult status: {result.status}", "doc_id": doc_id}
                else:
                    # status == "success"
                    # Record change for frontend changelog polling (best-effort)
                    try:
                        from niu_api.internal.lightrag_manager import get_change_log
                        get_change_log().record_change("document_deleted", {"id": doc_id})
                    except Exception as e:
                        logger.debug(f"changelog record_change failed: {e}")
                    return {"status": "ok", "doc_id": doc_id, "result": str(result)}
            else:
                # No status attribute — assume success for backward compatibility
                try:
                    from niu_api.internal.lightrag_manager import get_change_log
                    get_change_log().record_change("document_deleted", {"id": doc_id})
                except Exception as e:
                    logger.debug(f"changelog record_change failed: {e}")
                return {"status": "ok", "doc_id": doc_id, "result": str(result)}
        except Exception as e:
            err_str = str(e).lower()
            if "not found" in err_str or "404" in err_str:
                logger.info(f"LightRAG delete_document: doc '{doc_id}' not found, treated as ok")
                return {"status": "ok", "doc_id": doc_id, "note": "not_found_treated_as_ok"}
            logger.error(f"LightRAG delete_document failed: {e}")
            return {"status": "error", "message": str(e)}

    def delete_entity(self, entity_name: str) -> dict[str, Any]:
        """Delete an entity and all its relations from the knowledge graph.

        Args:
            entity_name: Entity name to delete.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            # call_async runs in LightRAG's asyncio loop (serialized there),
            # so concurrent writes are impossible. No write lock needed —
            # readers use graph_read_lock + copy() snapshot to avoid
            # RuntimeError("Graph changed during iteration").
            result = call_async(rag.adelete_by_entity(entity_name), timeout=lightrag_timeout("delete_timeout", 300))

            # Check DeletionResult status (LightRAG returns a DeletionResult object)
            if hasattr(result, "status"):
                if result.status == "not_found" or (hasattr(result, "status_code") and getattr(result, "status_code", None) == 404):
                    logger.info(f"LightRAG delete_entity: entity '{entity_name}' not found, treated as ok")
                    return {"status": "ok", "entity_name": entity_name, "note": "not_found_treated_as_ok"}
                elif result.status in ("not_allowed", "fail"):
                    logger.warning(f"LightRAG delete_entity failed for '{entity_name}': status={result.status}")
                    return {"status": "error", "message": f"DeletionResult status: {result.status}", "entity_name": entity_name}
                else:
                    # status == "success"
                    # Record change for frontend changelog polling (best-effort)
                    try:
                        from niu_api.internal.lightrag_manager import get_change_log

                        get_change_log().record_change("entity_deleted", {"id": entity_name})
                    except Exception as e:
                        logger.debug(f"changelog record_change failed: {e}")

                    return {"status": "ok", "entity_name": entity_name, "result": str(result)}
            elif hasattr(result, "status_code") and getattr(result, "status_code", None) == 404:
                logger.info(f"LightRAG delete_entity: entity '{entity_name}' not found (404), treated as ok")
                return {"status": "ok", "entity_name": entity_name, "note": "not_found_treated_as_ok"}
            else:
                # No status attribute — assume success for backward compatibility
                # Record change for frontend changelog polling (best-effort)
                try:
                    from niu_api.internal.lightrag_manager import get_change_log

                    get_change_log().record_change("entity_deleted", {"id": entity_name})
                except Exception as e:
                    logger.debug(f"changelog record_change failed: {e}")

                return {"status": "ok", "entity_name": entity_name, "result": str(result)}
        except Exception as e:
            err_str = str(e).lower()
            if "not found" in err_str or "404" in err_str:
                logger.info(f"LightRAG delete_entity: entity '{entity_name}' not found, treated as ok")
                return {"status": "ok", "entity_name": entity_name, "note": "not_found_treated_as_ok"}
            logger.error(f"LightRAG delete_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def edit_entity(self, entity_name, updated_data, allow_rename=False, allow_merge=False, timeout=300):
        """Edit an entity's properties in the knowledge graph.

        Args:
            entity_name: Entity name to edit.
            updated_data: Dict of properties to update.
            allow_rename: Whether to allow renaming the entity.
            allow_merge: Whether to allow merging with existing entities.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        # T3 守卫（外层 try 之前，I-10）：覆盖直接输入（改名/合流分支新旧两个名字）
        # 与 allow_rename 派生的级联重嵌逐条计量，公式见 _guard_edit_entity_entry。
        _guard_edit_entity_entry(rag, entity_name, updated_data, allow_rename, allow_merge)
        try:
            result = call_async(rag.aedit_entity(entity_name, updated_data, allow_rename=allow_rename, allow_merge=allow_merge), timeout)
            # Clean <SEP> from description in returned data
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG edit_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def edit_relation(self, source_entity, target_entity, updated_data, timeout=300):
        """Edit a relation's properties in the knowledge graph.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            updated_data: Dict of properties to update.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        # T3 守卫（外层 try 之前，I-10）：aedit_relation 实际嵌入
        # `source + "\t" + target + "\n" + keywords + "\n" + description`
        # （utils_graph.py:824，keywords 在第三段——与 create_relation /
        # inject_custom_kg 的第一段形态不同，v1.6 双审 A-P1）。
        # keywords/description 未提供的字段取存储侧（历史写入已受守卫约束），
        # 这里只计量调用方本次传入的部分。
        _guard_embed_text(
            "relation",
            f"{source_entity}->{target_entity}",
            f"{source_entity}\t{target_entity}\n"
            f"{updated_data.get('keywords') or ''}\n{updated_data.get('description') or ''}",
        )
        try:
            result = call_async(rag.aedit_relation(source_entity, target_entity, updated_data), timeout)
            # Clean <SEP> from description in returned data
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG edit_relation failed: {e}")
            return {"status": "error", "message": str(e)}

    def delete_relation(self, source_entity, target_entity, timeout=300):
        """Delete a relation from the knowledge graph.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.adelete_by_relation(source_entity, target_entity), timeout)
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG delete_relation failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_entity_info(self, entity_name, include_vector_data=False, timeout=30):
        """Get detailed information about an entity.

        Args:
            entity_name: Entity name to look up.
            include_vector_data: Whether to include vector data in the result.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.get_entity_info(entity_name, include_vector_data), timeout)
            # Clean <SEP> from description in result
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG get_entity_info failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_relation_info(self, source_entity, target_entity, include_vector_data=False, timeout=30):
        """Get detailed information about a relation.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            include_vector_data: Whether to include vector data in the result.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.get_relation_info(source_entity, target_entity, include_vector_data), timeout)
            # Clean <SEP> from description in result
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG get_relation_info failed: {e}")
            return {"status": "error", "message": str(e)}

    def create_entity(self, entity_name, entity_type, description="", source_id="manual_creation", file_path="manual_creation", timeout=300):
        """Create a new entity in the knowledge graph.

        Args:
            entity_name: Name of the entity to create.
            entity_type: Type of the entity (e.g., "Person", "Concept").
            description: Description of the entity.
            source_id: Source chunk ID.
            file_path: File path for citation.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        # T3 守卫（外层 try 之前，I-10）：acreate_entity 实际嵌入
        # `entity_name + "\n" + description`（utils_graph.py:994）；实体名另过
        # D-3.1 快速失败层。
        _check_entity_name_length(entity_name)
        _guard_embed_text("entity", entity_name, f"{entity_name}\n{description or ''}")
        try:
            entity_data = {"entity_type": entity_type, "description": description, "source_id": source_id, "file_path": file_path}
            result = call_async(rag.acreate_entity(entity_name, entity_data), timeout)
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG create_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def create_relation(self, source_entity, target_entity, keywords, description="", weight=1.0, source_id="manual_creation", file_path="manual_creation", timeout=300):
        """Create a new relation in the knowledge graph.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            keywords: Keywords/relation type for the edge.
            description: Description of the relation.
            weight: Weight of the relation (default 1.0).
            source_id: Source chunk ID.
            file_path: File path for citation.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        # T3 守卫（外层 try 之前，I-10）：acreate_relation 实际嵌入
        # `keywords + "\t" + src + "\n" + tgt + "\n" + description`
        # （utils_graph.py:1135，src/tgt 为排序后的端点，token 数与顺序无关）。
        _guard_embed_text(
            "relation",
            f"{source_entity}->{target_entity}",
            f"{keywords or ''}\t{source_entity}\n{target_entity}\n{description or ''}",
        )
        try:
            relation_data = {"keywords": keywords, "description": description, "weight": weight, "source_id": source_id, "file_path": file_path}
            result = call_async(rag.acreate_relation(source_entity, target_entity, relation_data), timeout)
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG create_relation failed: {e}")
            return {"status": "error", "message": str(e)}

    def document_status(self) -> dict[str, Any]:
        """Get document processing status counts.

        Returns:
            Dict with pending, processing, processed, failed counts.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            return call_async(rag.get_processing_status(), timeout=lightrag_timeout("status_timeout", 30))
        except Exception as e:
            logger.error(f"LightRAG document_status failed: {e}")
            return {"status": "error", "message": str(e)}

    def list_entities(
        self,
        list_type: str = "entities",
        entity_type: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """List entities or documents in the knowledge base.

        Args:
            list_type: "entities", "documents", or "labels".
            entity_type: Filter by entity type.
            limit: Max results.

        Returns:
            Dict with status and data (list of entities/documents/labels).
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            if list_type == "labels":
                data = call_async(rag.get_graph_labels(), timeout=lightrag_timeout("status_timeout", 30))
                return {"status": "ok", "data": data}
            elif list_type == "documents":
                data = call_async(rag.get_docs_by_status("processed"), timeout=lightrag_timeout("status_timeout", 30))
                return {"status": "ok", "data": data}
            elif list_type == "entities":
                if entity_type:
                    # 按 entity_type 过滤：直接遍历 NetworkX 图节点
                    # 不能用 get_knowledge_graph(entity_type)，因为那是按节点名搜索
                    from niu_api.internal.lightrag_manager import graph_read_lock

                    graph_obj = rag.chunk_entity_relation_graph
                    nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                    if nx_graph is None:
                        return {"status": "ok", "data": []}
                    nodes = []
                    with graph_read_lock():
                        snapshot = nx_graph.copy()
                    for node_id, node_data in snapshot.nodes(data=True):
                        nt = node_data.get("entity_type", "other")
                        if nt.lower() == entity_type.lower():
                            nodes.append({
                                "entity_name": node_id,
                                "entity_type": nt,
                                "description": _clean_description(node_data.get("description", ""), nt),
                                "source_id": node_data.get("source_id", ""),
                                "file_path": node_data.get("file_path", ""),
                            })
                            if len(nodes) >= limit:
                                break
                    return {"status": "ok", "data": nodes}
                else:
                    # 无过滤：用 get_knowledge_graph 全图搜索
                    kg = call_async(
                        rag.get_knowledge_graph(
                            "*",
                            max_depth=1,
                            max_nodes=limit,
                        ),
                        timeout=lightrag_timeout("query_timeout", 120),
                    )
                    if kg is None:
                        return {"status": "ok", "data": []}
                    nodes = []
                    for node in kg.nodes:
                        nodes.append({
                            "entity_name": node.id,
                            "entity_type": node.properties.get("entity_type", "other"),
                            "description": _clean_description(node.properties.get("description", ""), node.properties.get("entity_type", "other")),
                            "source_id": node.properties.get("source_id", ""),
                            "file_path": node.properties.get("file_path", ""),
                        })
                    return {"status": "ok", "data": nodes}
            else:
                return {"status": "error", "message": f"Unknown list_type: {list_type}"}
        except Exception as e:
            logger.error(f"LightRAG list_entities failed: {e}")
            return {"status": "error", "message": str(e)}

    def list_entities_by_name_regex(self, pattern: str, limit: int = 200) -> dict[str, Any]:
        """按实体名称正则匹配列举实体（不依赖 entity_type，placeholder 也可命中）。

        会话实体 entity_type 混杂（event/session/unknown），必须按名称定位。
        """
        import re

        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
            nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
            if nx_graph is None:
                return {"status": "ok", "data": []}
            regex = re.compile(pattern)
            from niu_api.internal.lightrag_manager import graph_read_lock

            with graph_read_lock():
                snapshot = nx_graph.copy()
            nodes = []
            for node_id, node_data in snapshot.nodes(data=True):
                if regex.match(node_id):
                    nt = node_data.get("entity_type", "other")
                    nodes.append({
                        "entity_name": node_id,
                        "entity_type": nt,
                        "description": _clean_description(node_data.get("description", ""), nt),
                    })
            # 按名称排序后取最近 limit 个——名称=日期，字典序尾即最新
            # （插入序截断会丢最新实体，破坏 10 天窗口保证）
            nodes.sort(key=lambda n: n["entity_name"])
            return {"status": "ok", "data": nodes[-limit:] if limit > 0 else []}
        except Exception as e:
            logger.error(f"LightRAG list_entities_by_name_regex failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_edge_keywords_between(self, src_id: str, tgt_id: str) -> list[str]:
        """返回两实体之间所有边的 keywords 列表（断边安全检查用）。

        LightRAG 的 delete_relation 无 keywords 过滤、删两实体间所有边，
        断边前必须确认无其他语义边。
        """
        rag = self._get_rag()
        if rag is None:
            return []
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return []
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        from niu_api.internal.lightrag_manager import graph_read_lock

        with graph_read_lock():
            src, tgt = src_id.lower(), tgt_id.lower()
            if not nx_graph.has_edge(src, tgt):
                return []
            edge_data = nx_graph.get_edge_data(src, tgt)
            if isinstance(edge_data, dict):
                return [str(edge_data.get("keywords", ""))]
            if isinstance(edge_data, list):
                return [str(e.get("keywords", "")) for e in edge_data if isinstance(e, dict)]
            return []

    def _resolve_entity_name_case_insensitive(
        self, entity_name: str, nx_graph
    ) -> str | None:
        """Resolve entity name with case-insensitive fallback.

        Tries exact match first. If that fails, searches all graph nodes
        for a case-insensitive match. Returns the canonical name from the
        graph, or None if not found at all.

        Args:
            entity_name: The entity name to resolve.
            nx_graph: NetworkX graph instance (snapshot, not live graph).

        Returns:
            Canonical entity name from graph, or None if not found.
        """
        if nx_graph.has_node(entity_name):
            return entity_name

        logger.warning(
            "Entity '{}' not found in graph, trying case-insensitive match",
            entity_name,
        )
        entity_name_lower = entity_name.lower()
        for node_name in nx_graph.nodes():
            if node_name.lower() == entity_name_lower:
                logger.info(
                    "Case-insensitive match: '{}' resolved to '{}'",
                    entity_name,
                    node_name,
                )
                return node_name

        logger.error(
            "Entity '{}' not found in graph (case-insensitive also failed)",
            entity_name,
        )
        return None

    def merge_entities(
        self,
        source_entities: list[str],
        target_entity: str,
        merge_strategy: dict | None = None,
        target_entity_data: dict | None = None,
    ) -> dict[str, Any]:
        """Merge multiple entities into one, consolidating all relations.

        Args:
            source_entities: Entity names to merge.
            target_entity: Name of the merged target entity.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        # Resolve entity names with case-insensitive fallback.
        # Take a snapshot under read lock to prevent RuntimeError from
        # concurrent graph modification by background sync (lightrag_sync, region_sync).
        from niu_api.internal.lightrag_manager import graph_read_lock

        graph_obj = rag.chunk_entity_relation_graph
        if graph_obj is None:
            return {"status": "error", "message": "Knowledge graph not available"}
        with graph_read_lock():
            nx_graph = (graph_obj._graph.copy() if hasattr(graph_obj, "_graph") else graph_obj.copy())

        resolved_sources: list[str] = []
        unresolved_sources: list[str] = []
        for src in source_entities:
            resolved = self._resolve_entity_name_case_insensitive(src, nx_graph)
            if resolved is None:
                unresolved_sources.append(src)
            elif resolved != src:
                logger.info(
                    "Source entity '{}' resolved to '{}' (case-insensitive)",
                    src, resolved,
                )
                resolved_sources.append(resolved)
            else:
                resolved_sources.append(src)

        if unresolved_sources:
            return {
                "status": "error",
                "message": (
                    f"Source entities not found in graph "
                    f"(case-insensitive also failed): {unresolved_sources}"
                ),
            }

        resolved_target = self._resolve_entity_name_case_insensitive(
            target_entity, nx_graph
        )
        if resolved_target is None:
            return {
                "status": "error",
                "message": (
                    f"Target entity '{target_entity}' not found in graph "
                    f"(case-insensitive also failed)"
                ),
            }
        if resolved_target != target_entity:
            logger.info(
                "Target entity '{}' resolved to '{}' (case-insensitive)",
                target_entity, resolved_target,
            )

        # T3 守卫（外层 try 之前，I-10）：覆盖目标实体侧
        # `target_entity + "\n" + description` 与源/目标两侧关系的重嵌，公式见
        # _guard_merge_entities_entry。nx_graph 为上面已取得的只读快照。
        _guard_merge_entities_entry(
            resolved_sources, resolved_target, target_entity_data, nx_graph
        )

        try:
            # call_async runs in LightRAG's asyncio loop (serialized there),
            # so concurrent writes are impossible. No write lock needed —
            # readers use graph_read_lock + copy() snapshot to avoid
            # RuntimeError("Graph changed during iteration").
            result = call_async(
                rag.amerge_entities(resolved_sources, resolved_target, merge_strategy=merge_strategy, target_entity_data=target_entity_data),
                timeout=lightrag_timeout("merge_timeout", 300),
            )

            # Record change for frontend changelog polling (best-effort)
            try:
                from niu_api.internal.lightrag_manager import get_change_log, graph_read_lock

                # Read target entity's actual attributes from the graph
                # Use resolved_target (canonical graph name) instead of the
                # raw target_entity which may differ by case.
                graph_obj = rag.chunk_entity_relation_graph
                nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                target_type = "other"
                target_desc = ""
                if nx_graph and nx_graph.has_node(resolved_target):
                    with graph_read_lock():
                        if nx_graph.has_node(resolved_target):
                            attrs = nx_graph.nodes[resolved_target]
                            target_type = attrs.get("entity_type", "other")
                            target_desc = _clean_description(attrs.get("description", ""), target_type)

                get_change_log().record_change("entity_merged", {
                    "source_ids": resolved_sources,
                    "target_id": resolved_target,
                    "name": resolved_target,
                    "type": target_type,
                    "description": target_desc,
                })
            except Exception as e:
                logger.debug(f"changelog record_change failed: {e}")

            return {"status": "ok", "target_entity": resolved_target, "result": str(result)}
        except AttributeError:
            return {"status": "error", "message": "Entity merge not supported by this LightRAG version"}
        except Exception as e:
            logger.error(f"LightRAG merge_entities failed: {e}")
            return {"status": "error", "message": str(e)}


class LightRAGIngester:
    """Data injection for LightRAG.

    Recommended: lightrag_insert → calls ainsert() for automatic entity/relationship extraction and merging.

    Structured: inject_custom_kg → calls ainsert_custom_kg() for precise entity/relationship injection (no LLM extraction).
    Complementary: inject_custom_kg (precise control) and lightrag_insert (auto extraction) serve different purposes.
    Unstructured: inject_document, inject_documents → calls ainsert() for raw document ingestion.
    """

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    def inject_custom_kg(
        self,
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        source_id: str = "custom_kg",
    ) -> dict[str, Any]:
        """Inject structured knowledge via ainsert_custom_kg().

        This is the primary structured injection method. It builds the
        custom_kg dict that LightRAG expects and calls ainsert_custom_kg.

        Complementary to lightrag_insert: use inject_custom_kg when you need
        precise control over entity names, relationship types, and graph structure
        (e.g., brain regions, photo metadata, person nodes). Use lightrag_insert
        for natural language content where LLM auto-extraction and entity merging
        are desired.

        Args:
            entities: List of entity dicts with keys:
                name, entity_type, description, source_id (optional), file_path (optional)
            relationships: List of relationship dicts with keys:
                src_id, tgt_id, relation, description (optional), source_id (optional), file_path (optional)
            chunks: List of chunk dicts with keys:
                content, source_id, file_path (optional)
            source_id: Default source ID for items without explicit source.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        # T3 守卫：必须在下面外层 try **之前**（本方法末尾有
        # `except Exception → {"status": "error"}` 兜底，raise 落在 try 内会被吞，
        # I-10 不成立）。返回的 chunks 列表已含超限 chunk 的分段子块；调用方传入的
        # chunks 列表本身不被就地修改（I-6）。
        chunks = _guard_inject_custom_kg_inputs(entities, relationships, chunks, source_id)

        try:
            # Build custom_kg dict matching LightRAG's expected structure
            custom_kg: dict[str, Any] = {
                "chunks": [],
                "entities": [],
                "relationships": [],
            }

            for chunk in chunks:
                custom_kg["chunks"].append({
                    "content": chunk["content"],
                    "source_id": chunk.get("source_id", source_id),
                    "file_path": chunk.get("file_path", "custom_kg"),
                    "chunk_order_index": chunk.get("chunk_order_index", 0),
                })

            # Auto-generate virtual chunks for source_ids not covered by existing
            # chunks. LightRAG's ainsert_custom_kg uses chunk_to_source_map to
            # translate source_id to chunk physical IDs. Without a mapping, all
            # source_ids become "UNKNOWN". Virtual chunks also improve vector recall.
            covered_source_ids: set[str] = {
                c.get("source_id", source_id) for c in custom_kg["chunks"]
            }
            for entity in entities:
                entity_source_id = entity.get("source_id", source_id)
                # Use unique source_id per entity to avoid chunk_to_source_map
                # overwriting (all entities sharing "brain" would map to the
                # same last chunk_id otherwise)
                unique_source_id = f"{entity_source_id}_{entity.get('entity_name') or entity.get('name', 'Other')}"
                if unique_source_id not in covered_source_ids:
                    entity_name = entity.get("entity_name") or entity.get("name", "Other")
                    entity_desc = entity.get("description", "")
                    # 虚拟 chunk 编号 = 内容哈希（上游 compute_mdhash_id），upsert 只覆盖不删旧编号。
                    # 脑区 description 含每次同步都变的 brain_meta_* 字段 ⇒ 不剔除会导致
                    # 编号每次漂移、chunk 条数只增不减（每次启动 +7 条）。剔除后内容稳定。
                    # 仅改虚拟 chunk 的投影文本；实体图节点 description 原样保留（见
                    # custom_kg["entities"]），brain_meta_* 下游消费方读图节点不受影响。
                    stripped = _strip_brain_meta(entity_desc)
                    custom_kg["chunks"].append({
                        "content": f"{entity_name}: {stripped}" if stripped else entity_name,
                        "source_id": unique_source_id,
                        "file_path": entity.get("file_path", "custom_kg"),
                        "chunk_order_index": 0,
                    })
                    covered_source_ids.add(unique_source_id)
                # Update entity's source_id to match the unique chunk source_id
                entity["source_id"] = unique_source_id
            # Guard: filter out illegal Niu -> non-brainregion connections.
            # Legal Niu -> brain-region anchor edges are created by the
            # region_manager independently (source_id="brain"), not via this
            # path. This guard intercepts any relationship whose src or tgt
            # is "Niu" (case-insensitive) and whose other end is not a
            # brain-region entity, preventing future code paths from
            # reintroducing the 24-rule-violating edges.
            filtered_relationships: list[dict[str, Any]] = []
            for rel in relationships:
                src = (rel.get("src_id") or "").strip()
                tgt = (rel.get("tgt_id") or "").strip()
                src_is_niu = src.lower() == "niu"
                tgt_is_niu = tgt.lower() == "niu"
                if src_is_niu or tgt_is_niu:
                    other_id = tgt if src_is_niu else src
                    other_entity = next(
                        (e for e in entities if (e.get("entity_name") or e.get("name")) == other_id),
                        None,
                    )
                    other_type = (other_entity.get("entity_type") or "").lower() if other_entity else ""
                    if other_type != "brainregion":
                        logger.warning(
                            f"[inject_custom_kg] 拦截违规 Niu 连接: {src} --[{rel.get('keywords','')}]--> {tgt} "
                            f"(对端 entity_type={other_type or '未知'}, 非 brainregion)"
                        )
                        continue
                filtered_relationships.append(rel)
            relationships = filtered_relationships

            for rel in relationships:
                rel_source_id = rel.get("source_id", source_id)
                if rel_source_id not in covered_source_ids:
                    src = rel.get('src_id', 'unknown')
                    tgt = rel.get('tgt_id', 'unknown')
                    keywords = rel.get('keywords', '') or rel.get('relation', '')
                    content = f"{src}->{tgt}: {keywords}" if keywords else f"{src} -> {tgt}"
                    custom_kg["chunks"].append({
                        "content": content,
                        "source_id": rel_source_id,
                        "file_path": rel.get("file_path", "custom_kg"),
                        "chunk_order_index": 0,
                    })
                    covered_source_ids.add(rel_source_id)

            for entity in entities:
                custom_kg["entities"].append({
                    "entity_name": entity.get("entity_name") or entity.get("name", "Other"),
                    "entity_type": entity.get("entity_type", "other"),
                    "description": entity.get("description", ""),
                    "source_id": entity.get("source_id", source_id),
                    "file_path": entity.get("file_path", "custom_kg"),
                })

            for rel in relationships:
                # LightRAG requires "keywords" (direct access, no .get() fallback)
                # and reads "description" (also direct access).
                # "weight" uses .get() with default 1.0.
                keywords = rel.get("keywords") or rel.get("relation", "")
                custom_kg["relationships"].append({
                    "src_id": rel["src_id"],
                    "tgt_id": rel["tgt_id"],
                    "keywords": keywords,
                    "description": rel.get("description", ""),
                    "source_id": rel.get("source_id", source_id),
                    "file_path": rel.get("file_path", "custom_kg"),
                    "weight": rel.get("weight", 1.0),
                })

            # call_async runs in LightRAG's asyncio loop (serialized there),
            # so concurrent writes are impossible. No write lock needed —
            # readers use graph_read_lock + copy() snapshot to avoid
            # RuntimeError("Graph changed during iteration").
            call_async(rag.ainsert_custom_kg(custom_kg), timeout=lightrag_timeout("insert_timeout", 600))

            # Record changes for frontend changelog polling
            # (best-effort: never let changelog errors affect the write result)
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                change_log = get_change_log()
                for entity in custom_kg["entities"]:
                    change_log.record_change("entity_created", {
                        "id": entity['entity_name'],
                        "name": entity["entity_name"],
                        "type": entity.get("entity_type", "other"),
                        "description": entity.get("description", ""),
                        "file_path": entity.get("file_path", ""),
                        "source_id": entity.get("source_id", ""),
                    })
                for rel in custom_kg["relationships"]:
                    change_log.record_change("edge_created", {
                        "source": rel['src_id'],
                        "target": rel['tgt_id'],
                        "relation": rel.get("keywords", ""),
                        "confidence": rel.get("weight", 1.0),
                    })
            except Exception as e:
                logger.debug(f"changelog record_change failed: {e}")

            return {
                "status": "ok",
                "entities": len(custom_kg["entities"]),
                "relationships": len(custom_kg["relationships"]),
                "chunks": len(custom_kg["chunks"]),
            }
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG custom_kg injection failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}

    def lightrag_insert(self, content: str, file_paths: str | None = None) -> dict[str, Any]:
        """通过 ainsert 入库结构化文本（LightRAG 自动提取实体/关系）。

        与 inject_custom_kg 互补：lightrag_insert 适用于自然语言内容，LightRAG
        会自动提取实体和关系、合并同名实体、建立实体之间的边。inject_custom_kg
        适用于需要精确控制实体名称、关系类型和图结构的场景（如脑区、照片元数据、人物节点）。

        Args:
            content: 结构化文本，格式化好的照片/人物/记忆描述
            file_paths: 可选的文件路径关联

        Returns:
            Dict[str, Any]: 成功时 {"status": "ok", "track_id": str}，失败时 {"status": "error", "message": str}
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            kwargs = {}
            if file_paths:
                kwargs["file_paths"] = file_paths
            track_id = call_async(rag.ainsert(content, **kwargs), timeout=lightrag_timeout("insert_timeout", 600))

            # Record change for frontend changelog polling (best-effort)
            # LLM-extracted entities are unknown at this point,
            # frontend should re-fetch full snapshot after receiving this event.
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("document_created", {
                    "id": track_id or "",
                    "uri": file_paths or "",
                    "title": file_paths or "lightrag_insert",
                    "source": "lightrag_insert",
                })
            except Exception as e:
                logger.debug(f"lightrag_insert changelog record failed: {e}")

            return {"status": "ok", "track_id": track_id}
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG lightrag_insert failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}

    # ============== Unstructured Path ==============

    def inject_document(
        self,
        content: str,
        doc_id: str | None = None,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        """Inject an unstructured document for LLM-driven entity extraction.

        Args:
            content: Document text content.
            doc_id: Optional unique document ID for dedup.
            file_path: Optional file path for citation.

        Returns:
            Dict with status and track_id.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            kwargs = {}
            if doc_id is not None:
                kwargs["ids"] = doc_id
            if file_path is not None:
                kwargs["file_paths"] = file_path

            track_id = call_async(rag.ainsert(content, **kwargs), timeout=lightrag_timeout("insert_timeout", 600))

            # Record document insertion for frontend changelog
            # (LLM-extracted entities are unknown, so frontend should
            # re-fetch full snapshot after receiving this event)
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("document_created", {
                    "id": doc_id or track_id or "",
                    "uri": file_path or "",
                    "title": file_path or doc_id or track_id or "",
                    "source": "inject_document",
                })
            except Exception as e:
                logger.debug(f"inject_document changelog record failed: {e}")

            return {"status": "ok", "track_id": track_id}
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG document injection failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}

    def inject_documents(
        self,
        documents: list[str],
        ids: list[str] | None = None,
        file_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Batch inject multiple unstructured documents.

        Args:
            documents: List of document text strings.
            ids: Optional list of unique document IDs.
            file_paths: Optional list of file paths.

        Returns:
            Dict with status and track_id.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            kwargs = {}
            if ids is not None:
                kwargs["ids"] = ids
            if file_paths is not None:
                kwargs["file_paths"] = file_paths

            track_id = call_async(rag.ainsert(documents, **kwargs), timeout=lightrag_timeout("insert_timeout", 600))

            # Record document insertion for frontend changelog
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("document_created", {
                    "id": ",".join(ids) if ids else "",
                    "uri": ",".join(file_paths) if file_paths else "",
                    "title": ",".join(file_paths) if file_paths else ",".join(ids) if ids else "",
                    "source": "inject_documents",
                    "count": len(documents),
                })
            except Exception as e:
                logger.debug(f"inject_documents changelog record failed: {e}")

            return {"status": "ok", "track_id": track_id, "count": len(documents)}
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG batch document injection failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}
