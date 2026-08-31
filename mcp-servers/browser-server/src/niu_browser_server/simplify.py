"""elements 精简 + 响应大小控制。

dom_tree.js flatTreeToString 真实输出格式（实测日志）：
  [*][N]<tag attrs />          — 无文本自闭合
  [*][N]<tag attrs>text />     — 有文本自闭合（文本在 > 和 /> 之间）
  [*][N]<tag attrs>text        — 有文本无自闭合（/> 在续行末尾）
  独立纯文本行（无 <tag>）      — 非元素，跳过

精简规则：
  - 去冗余属性（id=、target=、name=、class=、style= 等）
  - 保留 placeholder（输入框提示）和 aria-label/title（兜底文本）
  - 空元素（无文本/placeholder/aria-label）过滤
  - 每行格式：[N] tag: text
"""

import os
import re
import time
from pathlib import Path

MD_DIR = os.path.join(os.path.expanduser("~"), ".niu", "tmp")

# dom_tree.js 真实元素行格式（双分支）：
# 分支1（有 /> 自闭合）：[*][N]<tag attrs>text />
# 分支2（无 /> 纯开标签）：[*][N]<tag attrs>text（/> 在续行末尾）
_RE_WITH_CLOSE = re.compile(
    r'[\t ]*(?:\*?)\[(\d+)\]<(\w+)([^>]*?)>([^<]*?)\s*/>'
)
_RE_OPEN_ONLY = re.compile(
    r'^[\t ]*(?:\*?)\[(\d+)\]<(\w+)([^>]*?)(?:>([^<]*))?$'
)

# 属性提取：placeholder / aria-label / title（兜底文本源）
_ATTR_RE = re.compile(r'(?:placeholder|aria-label|title)=["\']?([^"\'\s>]+)["\']?')


def simplify_elements(raw: str) -> str:
    """将 dom_tree.js 完整 elements 精简为 [N] tag: text。

    双分支正则 + 续行合并状态机，处理跨行文本元素。
    格式漂移（0行匹配）时回退原始串。

    Returns:
        精简字符串，每行一个元素。
    """
    results = []
    pending = None  # (idx, tag, accumulated_text) — 续行合并状态

    def _flush_pending():
        """完成当前 pending 元素，加入 results。"""
        nonlocal pending
        if pending is None:
            return
        idx, tag, text = pending
        pending = None
        text = text.strip()
        # 属性兜底
        if not text:
            # 从原始行重新提取属性（需保留 attrs）——简化处理：跳过
            pass
        if not text:
            return
        text = text.replace('\n', ' ').strip()
        if len(text) > 40:
            text = text[:37] + '...'
        results.append(f'[{idx}] {tag}: {text}')

    for line in raw.split('\n'):
        # 分支1：有 /> 自闭合
        m = _RE_WITH_CLOSE.search(line)
        if m:
            _flush_pending()
            idx, tag, attrs, text = m.group(1), m.group(2), m.group(3), m.group(4)
            text = (text or '').strip()
            if not text:
                for am in _ATTR_RE.finditer(attrs):
                    candidate = am.group(1).strip()
                    if candidate and len(candidate) > 1:
                        text = candidate
                        break
            if not text:
                continue
            text = text.replace('\n', ' ').strip()
            if len(text) > 40:
                text = text[:37] + '...'
            results.append(f'[{idx}] {tag}: {text}')
            continue

        # 分支2：无 /> 纯开标签（续行起始）
        m = _RE_OPEN_ONLY.search(line)
        if m:
            _flush_pending()
            idx, tag, attrs, text = m.group(1), m.group(2), m.group(3), m.group(4)
            text = (text or '').strip()
            if not text:
                for am in _ATTR_RE.finditer(attrs):
                    candidate = am.group(1).strip()
                    if candidate and len(candidate) > 1:
                        text = candidate
                        break
            pending = (idx, tag, text)
            continue

        # 续行：非元素行，合并到 pending
        if pending is not None and line.strip():
            idx, tag, text = pending
            pending = (idx, tag, (text + ' ' + line.strip()).strip())
            continue

    _flush_pending()

    if not results:
        return raw  # 格式漂移 → 回退原始串

    return '\n'.join(results)


def fit_response(data: dict, budget: int = 27000) -> dict:
    """控制响应总大小 < budget。

    流程：
    1. 计算固定开销（url + title + tabSummary + currentTabId + JSON结构）
    2. elements 预算 = budget - 固定开销 - 折叠标记开销
    3. 精简 elements
    4. 精简后仍超预算 → 截断 elements + 追加折叠标记 + 写临时文件
    5. 未超预算 → 原样返回

    Returns:
        调整后的 data dict（含 elementsFile 字段）
    """
    elements = data.get('elements', '')
    if not elements or len(elements) < 5000:
        return data  # 小响应，不处理

    # 固定开销估算
    fixed_cost = 50  # JSON结构开销
    for k in ['url', 'title', 'tabSummary', 'currentTabId']:
        fixed_cost += len(str(data.get(k, ''))) + 20  # 每字段+20（key名+引号+逗号）
    fixed_cost += 80  # pageInfo/status/message/elementsFile 等其他字段
    fold_marker_cost = 150  # 含临时文件路径
    elements_budget = budget - fixed_cost - fold_marker_cost

    # 精简 elements
    simplified = simplify_elements(elements)

    if len(simplified) <= elements_budget:
        data['elements'] = simplified
        return data

    # 截断：保留前 elements_budget 字符 + 折叠标记
    truncated = simplified[:elements_budget]
    last_newline = truncated.rfind('\n')
    if last_newline > 0:
        truncated = truncated[:last_newline]

    # 写完整 elements 到临时文件
    full_path = write_full_elements(elements)

    # 统计
    total_elements = len(_RE_WITH_CLOSE.findall(elements)) + len(_RE_OPEN_ONLY.findall(elements))
    kept_elements = len([l for l in truncated.split('\n') if l.strip()])

    fold_note = f'\n... 内容已折叠（显示 {kept_elements}/{total_elements} 个元素）。完整列表: {full_path or "(写入失败)"}'
    data['elements'] = truncated + fold_note
    data['elementsFile'] = full_path

    return data


def write_full_elements(raw: str, tag: str = "browser_state") -> str | None:
    """写完整 elements 到临时文件，返回文件路径。失败返回 None。"""
    try:
        os.makedirs(MD_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d%H%M%S%f")[:17]  # 毫秒精度防覆盖
        path = os.path.join(MD_DIR, f'{tag}_{ts}.txt')
        Path(path).write_text(raw, encoding='utf-8')
        return path
    except Exception:
        return None
