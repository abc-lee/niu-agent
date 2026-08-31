"""Browser 响应大小控制。

设计（2026-08-31 用户拍板）：
- elements 原样输出，不做任何精简/解析（dom_tree.js 给什么就是什么）
- 响应总大小接近 30K（MAX_TOOL_RESULT_CHARS）时，按行边界截断 elements，
  保证 tabSummary/currentTabId 等结构化字段完整可见
- 截断保留头部（搜索框/导航）+ 尾部（分页/提交按钮），中间折叠
- 截断处打折叠标记（含完整内容临时文件路径）
- 完整 elements 写 ~/.niu/tmp/browser_state_*.txt 供按需查看

零格式假设：elements 是纯字符串，只做行边界截断，不做内容理解——
任何网站的输出都安全（不存在格式解析漏洞）。

预算转义感知：json.dumps 中 \\n→\\\\n(+1)、"→\\"(+1)、\\→\\\\(+1)、
\\t/\\r(+1)——行成本上界 = len + count('"') + count('\\\\') + count('\\t') + count('\\r') + 2。

截断路径最终校验（审查 B P1-1）：truncated + fold_note 的总转义成本必须
≤ elements_budget；尾部行数从 30 起递减直至 fit——任何页面形态都不复发原 bug。
"""

import os
import time
import uuid
from pathlib import Path

MD_DIR = os.path.join(os.path.expanduser("~"), ".niu", "tmp")

# 安全余量：JSON 结构/disk 截断标记等不可预见开销
_SAFETY_MARGIN = 800
# 尾部保留行数（分页/表单提交按钮常在页面底部）
_TAIL_LINES = 30
# 折叠标记成本上限（fold_note 含文件路径）
_FOLD_MARKER_COST = 200


def fit_response(data: dict, budget: int = 27000) -> dict:
    """控制响应总大小 < budget。

    elements 原样输出；总大小（序列化后）超预算时按行截断 elements。

    流程：
    1. 计算固定字段（url/title/tabSummary/currentTabId/status/message/pageInfo）大小
    2. elements 预算 = budget - 固定字段 - 折叠标记 - 安全余量
    3. 总大小统一判断（审查 B P2-1）：elements 小但 tabSummary 巨大也走收缩
    4. 超预算 → 先写完整到临时文件 → 构造 fold_note → 头+尾截断（递减尾部至 fit）
    5. 未超预算 → 原样返回

    tabSummary/currentTabId 是 data dict 的结构化字段——截断只作用于
    elements 字符串内部，这两个字段天然完整（初始 bug 的根治）。

    Returns:
        调整后的 data dict（含可选 elementsFile 字段）
    """
    elements = data.get('elements', '')
    if not elements:
        return data

    # 固定字段大小（key 名 + 引号 + 逗号）
    fixed_cost = 60  # status/message/elementsFile 等固定键开销
    for k in ['url', 'title', 'tabSummary', 'currentTabId', 'pageInfo']:
        v = str(data.get(k, ''))
        fixed_cost += _json_size(v) + 20  # 每字段转义感知 +20（key 名+引号+逗号）
    elements_budget = budget - fixed_cost - _FOLD_MARKER_COST - _SAFETY_MARGIN

    if elements_budget <= 0:
        # 固定字段已占满预算（tabSummary 极端巨大）：elements 降级为 stub
        # （审查 B P2-1：透传会让总大小超 30K，收缩 elements 能省出空间）
        full_path = write_full_elements(elements)
        data['elements'] = (
            f'\n... [elements 超限，完整内容见 {full_path or "(写入失败)"}，用 read 工具查看]'
        )
        if full_path:
            data['elementsFile'] = full_path
        return data

    # 总大小统一判断（转义感知上界）——elements_budget 已扣 fixed_cost，
    # 此处只比 elements 自身成本，不得再加 fixed_cost（审查 C P1-1：双重扣减
    # 会让"本可透传"的响应走截断并追加虚假折叠标记）
    if _json_size(elements) <= elements_budget:
        return data  # 总大小安全，原样返回（不做任何修改）

    # 写完整 elements 到临时文件（fold_note 需要路径）
    full_path = write_full_elements(elements)

    # 占位符用最大值高估（审查 C P3-3 + D P3-3）：total_lines 与 kept_lines 均可
    # 为 4 位数（京东 1,450 行保留 ~1000+ 行），占位 9999/9999 使估算为真上界，
    # 防止最终校验因估算偏低误触发 stub 降级丢弃 head+tail
    fold_note = (
        f'\n\n... [内容已折叠：共 9999 行，显示前 9999 行'
        f'（含尾部 {_TAIL_LINES} 行）。完整内容见 {full_path or "(写入失败)"}，用 read 工具查看]'
    )
    fold_note_cost = _json_size(fold_note)
    inner_budget = elements_budget - fold_note_cost

    truncated, total_lines, kept_lines, tail_n = _truncate_head_tail(elements, inner_budget)

    # 填实际行数（fold_note 在截断后构造以携带准确行数；尾部用实际 tail_n——
    # 审查 E P3：尾部递减场景下 tail < 30，固定表述会误报）
    fold_note = (
        f'\n\n... [内容已折叠：共 {total_lines} 行，显示前 {kept_lines} 行'
        f'（含尾部 {tail_n} 行）。完整内容见 {full_path or "(写入失败)"}，用 read 工具查看]'
    )

    # 最终校验（审查 B P1-1）：防御性——不满足则降级为最小 stub
    if _json_size(truncated + fold_note) > elements_budget:
        stub = f'\n... [内容超限，完整内容见 {full_path or "(写入失败)"}]'
        truncated = stub
        fold_note = ''

    data['elements'] = truncated + fold_note
    if full_path:
        data['elementsFile'] = full_path  # 写失败（None）时省略键，不产生 JSON null

    return data


def _json_size(s: str) -> int:
    """JSON 序列化后字符串的字符数上界（实际值 ≤ 此值）。

    \\n→\\\\n(+1)、"→\\"(+1)、\\→\\\\(+1)、\\t/\\r→\\\\t/\\\\r(+1)，其余字符 1:1。
    +2 为 json.dumps 首尾包裹引号（契约：len(json.dumps(s)) ≤ 本值）。
    例外（审查 E P3）：\\b/\\f/其他 C0 控制字符的 json.dumps 转义（+1/+5）
    未计入——页面文本现实无此字符，_SAFETY_MARGIN=800 兜底，无功能风险。
    """
    return len(s) + s.count('"') + s.count('\\') + s.count('\t') + s.count('\r') + s.count('\n') + 2


def _line_cost(line: str) -> int:
    """单行 JSON 转义感知成本（上界）：len + 转义字符 + 换行符(JSON \\\\n=2)。"""
    return len(line) + line.count('"') + line.count('\\') + line.count('\t') + line.count('\r') + 2

def _truncate_head_tail(text: str, budget: int) -> tuple[str, int, int, int]:
    """保留头部 + 尾部行，中间折叠（转义感知预算，不切行中间）。

    算法（审查 B P1-1 修复）：
    1. 无 ≤60 行早退——所有页面形态都做预算核算
    2. 尾行数从 _TAIL_LINES 起；尾部成本超预算时递减尾部给头部腾空间
    3. 头部逐行累积至 head_budget；首行超预算则保尾放弃头（head 空）
    4. 尾部递减到 0 仍放不下头 → 仅保留折叠标记（总成本仍 ≤ budget）

    Returns:
        (截断后的字符串, 原始总行数, 保留行数, 实际尾部行数)
    """
    lines = text.split('\n')
    total_lines = len(lines)
    if total_lines == 0:
        return '', 0, 0, 0

    tail_n = min(_TAIL_LINES, total_lines)
    head = []
    tail = []
    while True:
        tail = lines[total_lines - tail_n:] if tail_n > 0 else []
        tail_cost = sum(_line_cost(l) for l in tail)
        omitted = total_lines - tail_n
        marker = '\n... [中间省略 %d 行] ...\n' % omitted
        marker_cost = _json_size(marker) if omitted > 0 else 0
        head_budget = budget - tail_cost - marker_cost

        head = []
        cost = 0
        for line in lines[:omitted]:
            c = _line_cost(line)
            if cost + c > head_budget:
                break
            head.append(line)
            cost += c

        if head or tail_n == 0:
            break
        if head_budget > 0:
            break  # 首行超预算：保尾放弃头（tail+marker 已 ≤ budget）
        tail_n -= 1  # 尾部成本挤占头部空间：递减尾部重试

    head_n = len(head)
    kept_lines = head_n + tail_n
    omitted = total_lines - kept_lines
    parts = list(head)
    if omitted > 0:
        parts.append('... [中间省略 %d 行] ...' % omitted)
    parts.extend(tail)
    truncated = '\n'.join(parts)
    return truncated, total_lines, kept_lines, tail_n


def write_full_elements(raw: str, tag: str = "browser_state") -> str | None:
    """写完整 elements 到临时文件，返回文件路径。失败返回 None。"""
    try:
        os.makedirs(MD_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d%H%M%S")  # 秒级
        suffix = uuid.uuid4().hex[:4]  # 随机后缀防同秒覆盖
        path = os.path.join(MD_DIR, f'{tag}_{ts}_{suffix}.txt')
        Path(path).write_text(raw, encoding='utf-8')
        return path
    except Exception:
        return None
