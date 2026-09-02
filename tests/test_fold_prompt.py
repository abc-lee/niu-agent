"""Task 5 提示词断言：niu.md 并行工具调用教学 + 上下文折叠教学（spec §7）。

静态段教学边界：不写机制细节、不恐吓、可再生判定交给 LLM——这里只锁关键子串在场。
"""
from pathlib import Path

_root = Path(__file__).parent.parent
NIU_MD = _root / "config" / "agents" / "niu.md"


def test_niu_md_has_parallel_tool_call_teaching():
    """并行教学：无依赖的多工具调用应一轮全发（历史欠账——机制层原生支持，提示词从未教）。"""
    md = NIU_MD.read_text(encoding="utf-8")
    assert "## 并行工具调用与上下文折叠" in md, "niu.md 缺新节标题"
    assert "多个工具调用之间没有依赖关系时" in md, "缺并行教学句（触发条件）"
    assert "一次性发出全部调用" in md, "缺并行教学句（正确做法）"


def test_niu_md_has_fold_teaching():
    """折叠教学：仪表盘信号 + 可再生判定 + fold_tool_output + 搭车纪律。"""
    md = NIU_MD.read_text(encoding="utf-8")
    assert "fold_tool_output" in md, "缺 fold 工具名教学"
    assert "[输出#N · 工具名 · 占比]" in md, "缺头行标记形态教学"
    assert "搭车调用" in md, "缺搭车调用纪律"
    assert "绝不要只为折叠单开一轮" in md, "缺单开一轮禁令"
    assert "重新调用原工具即可" in md, "缺取回通道教学（重调原工具）"


def test_niu_md_fold_section_position():
    """插入位置：# 行为准则 区 ## 调用子 Agent 节后、## 通用子 Agent 节前。"""
    md = NIU_MD.read_text(encoding="utf-8")
    i_call = md.index("## 调用子 Agent")
    i_fold = md.index("## 并行工具调用与上下文折叠")
    i_generic = md.index("## 通用子 Agent")
    assert i_call < i_fold < i_generic, "新节位置错误（应在调用子 Agent 后、通用子 Agent 前）"
