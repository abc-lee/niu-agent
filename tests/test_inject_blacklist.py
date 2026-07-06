"""测试动态注入黑名单：brainregion 类型实体应被过滤，不注入到参考知识。

Bug 2: 脑区实体的 description 含 brain_meta_* 元数据（GraphML 限制下用 description
存元数据），被向量检索命中后原样注入到系统提示词的"参考知识"区，格式乱码。

修复策略：在 _INJECT_ENTITY_TYPE_BLACKLIST 加入 "brainregion"。

脑区信息不会丢失：
- 脑区状态（激活/调暗/成员数）通过 format_region_map_only 独立注入
- 脑区成员知识通过 search_within_region 检索
- 脑区 summary 已在脑区状态图中截取前 30 字符显示
"""
import sys
import os

# 确保能找到 agent 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.runner import NiuRunner


def test_inject_blacklist_contains_brainregion():
    """黑名单应包含 brainregion 类型，避免脑区 description 乱码注入参考知识"""
    assert "brainregion" in NiuRunner._INJECT_ENTITY_TYPE_BLACKLIST, (
        "brainregion 必须在 _INJECT_ENTITY_TYPE_BLACKLIST 中，"
        "否则脑区实体的 brain_meta_* 元数据会被当知识注入"
    )


def test_format_entities_filters_brainregion_entity():
    """brainregion 类型实体的 description（含 brain_meta_*）应被黑名单过滤，返回空字符串"""
    # 用 __new__ 跳过 __init__（不需要真实 LLM/MCP 依赖）
    runner = NiuRunner.__new__(NiuRunner)

    brainregion_entity = {
        "entity_name": "文档库脑区",
        "entity_type": "brainregion",
        "description": (
            "用户导入的文档和资料<SEP>brain_meta_region_id:default_文档库"
            "<SEP>brain_meta_size:119"
        ),
    }
    # 调 _format_lightrag_entities_for_prompt，title 用 "参考知识"（非技能区）
    text, seen = runner._format_lightrag_entities_for_prompt(
        [brainregion_entity], "参考知识", set()
    )
    # 断言被过滤，返回空字符串
    assert text == "", (
        f"brainregion 实体应被黑名单过滤，但返回非空: {text!r}"
    )
    # seen_names 不应包含被过滤的实体
    assert "文档库脑区" not in seen, (
        f"brainregion 实体不应被加入 seen_names: {seen}"
    )


def test_format_entities_keeps_normal_entity():
    """正常 knowledge 类型实体不应被误过滤（回归保护）"""
    runner = NiuRunner.__new__(NiuRunner)

    normal_entity = {
        "entity_name": "Python 编码规范",
        "entity_type": "knowledge",
        "description": "PEP 8 规定缩进用 4 空格<SEP>禁止 tab 混用",
    }
    text, seen = runner._format_lightrag_entities_for_prompt(
        [normal_entity], "参考知识", set()
    )
    assert text != "", "正常 knowledge 实体应被注入，不应被过滤"
    assert "Python 编码规范" in text
    assert "PEP 8" in text
    assert "Python 编码规范" in seen


def test_format_entities_filters_multiple_brainregion_entries():
    """多个 brainregion 实体都应被过滤"""
    runner = NiuRunner.__new__(NiuRunner)

    entities = [
        {
            "entity_name": "文档库脑区",
            "entity_type": "brainregion",
            "description": "用户文档<SEP>brain_meta_region_id:default_文档库",
        },
        {
            "entity_name": "笔记脑区",
            "entity_type": "BrainRegion",  # title case 也要过滤
            "description": "笔记<SEP>brain_meta_region_id:default_笔记",
        },
        {
            "entity_name": "正常知识",
            "entity_type": "knowledge",
            "description": "有用的知识",
        },
    ]
    text, seen = runner._format_lightrag_entities_for_prompt(
        entities, "参考知识", set()
    )
    # 只有"正常知识"应被注入
    assert "正常知识" in text, "正常 knowledge 实体应被注入"
    assert "文档库脑区" not in text, "brainregion 实体应被过滤"
    assert "笔记脑区" not in text, "brainregion 实体（title case）应被过滤"
    assert "brain_meta_" not in text, "brain_meta_* 元数据不应出现在注入文本中"
    assert seen == {"正常知识"}
