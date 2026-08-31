"""fit_response 大小控制单测——elements 原样输出 + 头尾截断保护。

核心语义（用户拍板 2026-08-31）：
- elements 不精简、不解析、原样输出
- 超预算按行截断（头+尾双保留），tabSummary/currentTabId 完整保留
- 截断处折叠标记 + 临时文件路径
- 预算转义感知 + 截断路径最终校验（审查 B P1-1），序列化后恒 < 30000
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "browser-server", "src"))

from niu_browser_server.simplify import (
    fit_response, write_full_elements, _truncate_head_tail, _json_size,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _make_elements(n_lines, quoted=False):
    """生成 n_lines 行 elements。quoted=True 时属性带引号（转义压力测试）。"""
    if quoted:
        body = '*[{i}]<a target="_blank" data-id="{i}">element {i} text content</a>'
    else:
        body = '*[{i}]<a target=_blank>element {i} text content here text content here />'
    return '\n'.join(body.format(i=i) for i in range(n_lines))


def _make_data(elements, tabsummary='| TabID | Title | URL |\n| 1 | 京东 | https://jd.com |'):
    return {
        'status': 'success', 'url': 'https://www.jd.com/', 'title': '京东',
        'elements': elements, 'pageInfo': {},
        'tabSummary': tabsummary, 'currentTabId': 1,
    }


def _assert_under_30k(result):
    total = len(json.dumps(result, ensure_ascii=False))
    assert total < 30000, f'response {total} chars exceeds 30K'


class TestFitResponse:
    def test_small_elements_passthrough(self):
        """小 elements 原样返回（不做任何修改）"""
        small = '*[0]<a >首页 />\n*[1]<button>搜索 />'
        data = _make_data(small)
        result = fit_response(data)
        assert result['elements'] == small  # 一个字节都不改
        assert result.get('elementsFile') is None

    def test_big_elements_truncated_tabsummary_kept(self, tmp_path, monkeypatch):
        """大 elements 截断，但 tabSummary/currentTabId 完整"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        big = _make_elements(2000)
        data = _make_data(big)
        result = fit_response(data)
        _assert_under_30k(result)
        # 核心：tabSummary/currentTabId 完整保留
        assert result['tabSummary'] == data['tabSummary']
        assert result['currentTabId'] == 1
        # elements 被截断 + 折叠标记
        assert '已折叠' in result['elements']
        assert len(result['elements']) < len(big)
        # 文件在 tmp_path 内（未污染真实 ~/.niu/tmp）
        assert result['elementsFile'] is not None
        assert result['elementsFile'].startswith(str(tmp_path))
        # 头部 + 尾部双保留
        assert '*[0]' in result['elements']            # 头部在场
        assert '*[1999]' in result['elements']         # 尾部在场（分页/提交按钮）
        assert '中间省略' in result['elements']        # 中间折叠标记

    def test_few_long_lines_no_early_return(self, tmp_path, monkeypatch):
        """≤60 行但单行超长（40 行×800 字符长文本）——审查 B P1-1 Case A"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        lines = '\n'.join(f'*[{i}]<p>{"x" * 780}</p>' for i in range(40))  # ~32K
        data = _make_data(lines)
        result = fit_response(data)
        _assert_under_30k(result)
        assert result['tabSummary'] == data['tabSummary']  # 不复发原 bug
        assert '已折叠' in result['elements']

    def test_huge_tabsummary_small_elements(self, tmp_path, monkeypatch):
        """tabSummary 真 >26K 触发 stub 分支（审查 B P2-1 + C P1-2 + D P1-NEW）"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        small = _make_elements(50)
        # 600 行实测 raw=26,093 → fixed_cost≈26.7K > 26K → elements_budget<0 → stub
        # 700 行会到 30.5K 越过 30K 上限（审查 D P1-NEW：stub 后总序列化 31,504 必挂）
        big_tabs = '| TabID | Title | URL |\n' + '\n'.join(
            f'| {i} | tab {i} | https://example.com/{i} |' for i in range(600)
        )  # ~26.1K；stub 后总序列化 ~27.0K < 30K
        data = _make_data(small, tabsummary=big_tabs)
        result = fit_response(data)
        _assert_under_30k(result)
        # tabSummary 完整 + elements 降级为 stub
        assert result['tabSummary'] == big_tabs
        assert 'elements 超限' in result['elements']
        assert result['elementsFile'].startswith(str(tmp_path))

    def test_passthrough_when_total_safe(self):
        """固定字段大但总大小安全 → 原样透传，无虚假折叠标记（审查 C P1-1）"""
        small = _make_elements(50)   # 实测 3,628 字符
        tabs = '| TabID | Title | URL |\n' + '\n'.join(
            f'| {i} | tab {i} | https://example.com/{i} |' for i in range(400)
        )  # 实测 17,293 字符；total ≈ 21.5K < 30K，正确账目下应透传（审查 D P3-2 修正注释）
        data = _make_data(small, tabsummary=tabs)
        result = fit_response(data)
        assert result['elements'] == small          # 原样透传
        assert '已折叠' not in result['elements']   # 无虚假折叠标记（P1-1 双重扣减会失败于此）
        assert result.get('elementsFile') is None

    def test_truncation_line_boundary(self):
        """截断不切行中间（按行边界）——审查 B P3-3：必须真正触发截断"""
        lines = ['0123456789abcdefghij'] * 100  # 100 行 × 20 字符
        text = '\n'.join(lines)
        truncated, total, kept, tail_n = _truncate_head_tail(text, 500)
        assert total == 100
        assert kept < 100  # 确实截断了
        body_lines = [l for l in truncated.split('\n') if not l.startswith('...')]
        assert all(len(l) == 20 for l in body_lines)  # 每行完整，无行中截断

    def test_elements_not_parsed_large_page(self, tmp_path, monkeypatch):
        """大 16P 页面真正走截断后 radio 行仍完整（不解析内容）——审查 B P2-4"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        # 实测 raw=25,744 / json_size=26,546（审查 F P3 精度修正）：超 elements_budget
        # (~25.8K) 734 字符，确定性触发头尾截断（纯字符串计算，无随机性）
        header = '*[0]<div >question container />\n' * 800
        radios = (
            '*[39]<input type=radio name=q1 value=-3 aria-label=I strongly agree />\n'
            '*[40]<input type=radio name=q1 value=-2 aria-label=I moderately agree />\n'
        )
        raw = header + radios
        assert len(raw) > 25000  # 确保超预算
        data = _make_data(raw, tabsummary='tabs')
        result = fit_response(data)
        _assert_under_30k(result)
        # radio 行在尾部保留区（末 2 行）——原样完整
        assert 'aria-label=I strongly agree' in result['elements']
        assert 'aria-label=I moderately agree' in result['elements']

    def test_tabsummary_protected_when_elements_huge(self, tmp_path, monkeypatch):
        """elements 极大时 tabSummary 仍保护（30K 内）"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        huge = _make_elements(3000, quoted=True)  # 带引号属性，转义压力
        data = _make_data(huge)
        result = fit_response(data)
        _assert_under_30k(result)
        assert result['currentTabId'] == 1
        assert '| 1 | 京东 |' in result['tabSummary']

    def test_quoted_attrs_escape_budget(self, tmp_path, monkeypatch):
        """带引号属性的短行页——转义后仍 <30K（审查 A P1 回归）"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        quoted = _make_elements(2500, quoted=True)  # 实测每行 61-70 字符（四位索引行更长；审查 F P3）
        data = _make_data(quoted)
        result = fit_response(data)
        _assert_under_30k(result)
        assert result['tabSummary'] == data['tabSummary']

    def test_real_jd_elements(self, tmp_path, monkeypatch):
        """京东真实 elements（33.9K）——截断 + tabSummary 完整 + <30K"""
        fixture = os.path.join(FIXTURE_DIR, 'browser_jd_elements.txt')
        if not os.path.exists(fixture):
            pytest.skip('京东真实 elements fixture 缺失（browser 会话不可用）')
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        with open(fixture, encoding='utf-8') as f:
            raw = f.read()
        # fixture 有效性：真实超 30K 样本（截断版会在此响亮失败——审查 B P2-2）
        assert 32000 < len(raw) < 36000, f'fixture 长度异常: {len(raw)}'
        data = _make_data(raw)
        result = fit_response(data)
        _assert_under_30k(result)
        assert result['tabSummary'] == data['tabSummary']
        assert '搜索' in result['elements']  # 京东搜索框在场（头部）

class TestWriteFullElements:
    def test_writes_full_content(self, tmp_path, monkeypatch):
        """临时文件写入完整 elements"""
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        content = '*[0]<a />\n*[1]<button>搜索 />'
        path = write_full_elements(content, tag='test_state')
        assert path is not None
        assert os.path.exists(path)
        with open(path, encoding='utf-8') as f:
            assert f.read() == content  # 完整内容

    def test_returns_none_on_error(self, tmp_path, monkeypatch):
        """makedirs 抛异常时返回 None（不崩溃）"""
        def _boom(*a, **kw):
            raise OSError('permission denied')
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path / 'x'))
        monkeypatch.setattr('niu_browser_server.simplify.os.makedirs', _boom)
        assert write_full_elements('x') is None


class TestJsonSize:
    def test_escape_aware(self):
        """_json_size 是序列化后大小的上界（含 \\t/\\r——审查 B P3-1）"""
        samples = [
            '*[0]<a >首页 />',                                    # 无引号
            '*[1]<a target="_blank">text with "quote" />',        # 引号
            'line with \\ backslash',                             # 反斜杠
            'multiline\nsecond line',                             # 换行
            'tab\tseparated\rvalue',                              # 控制字符
        ]
        for s in samples:
            assert _json_size(s) >= len(json.dumps(s, ensure_ascii=False))
