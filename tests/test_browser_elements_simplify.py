"""elements 精简函数单测——真实 dom_tree.js 格式 fixture。

真实格式：自闭合 /> 在行尾，文本在 > 和 /> 之间，无闭合标签。
fixture 来源：~/.niu/logs/raw_http/20260831/000214_request.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "browser-server", "src"))

from niu_browser_server.simplify import simplify_elements, fit_response, write_full_elements


# dom_tree.js 真实输出格式 fixture（从实际日志提取）
# 格式：*[N]<tag attrs>text /> 或 *[N]<tag attrs />
REAL_FORMAT = """*[0]<a id=result_logo />
*[1]<textarea id=chat-textarea>黄豆芽 营养成分 每100克 热量 蛋白质 膳食纤维 />
*[2]<div  />
*[7]<button id=chat-submit-button>百度一下 />
*[8]<a >百度首页 />
*[12]<a name=tj_settingicon>设置 />
*[13]<a id=user>laelee />
*[16]<a target=_self>图片 />
*[26]<div >黄豆芽营养成分表100g />
*[225]<a >下一页\xa0> />"""


class TestSimplifyElements:
    def test_strips_redundant_attributes(self):
        result = simplify_elements(REAL_FORMAT)
        assert '[7] button: 百度一下' in result
        assert '[12] a: 设置' in result
        assert '[16] a: 图片' in result
        assert 'id=' not in result
        assert 'target=' not in result
        assert 'name=' not in result

    def test_self_closing_tag_cleaned(self):
        result = simplify_elements(REAL_FORMAT)
        assert '/>' not in result

    def test_empty_element_filtered(self):
        result = simplify_elements(REAL_FORMAT)
        assert '[0]' not in result
        assert '[2]' not in result

    def test_text_preserved(self):
        result = simplify_elements(REAL_FORMAT)
        assert '[7] button: 百度一下' in result
        assert '[8] a: 百度首页' in result
        assert '[26] div: 黄豆芽营养成分表100g' in result

    def test_plain_text_lines_skipped(self):
        result = simplify_elements(REAL_FORMAT)
        lines = result.split('\n')
        for line in lines:
            assert line.startswith('['), f'非元素行残留: {line}'

    def test_multiline_text_in_element(self):
        """多行文本——开标签无 />，续行为文本，末行 />"""
        raw = '*[1]<textarea>\n第一行\n第二行\n第三行 />'
        result = simplify_elements(raw)
        assert '[1] textarea: 第一行 第二行 第三行' in result

    def test_long_text_truncated(self):
        long_text = 'x' * 60
        raw = f'*[0]<a>{long_text} />'
        result = simplify_elements(raw)
        line = result.strip()
        assert len(line) < 60

    def test_size_reduction(self):
        lines = [f'*[{i}]<a id=item{i} target=_blank role=button>item{i} text here />' for i in range(1500)]
        raw = '\n'.join(lines)
        result = simplify_elements(raw)
        assert len(result) < 50000
        assert len(result) < len(raw) * 0.5

    def test_zero_match_fallback(self):
        raw = 'completely unexpected format'
        result = simplify_elements(raw)
        assert result == raw

    def test_real_format_simplification(self):
        """真实格式 274 元素精简后 <10K"""
        lines = [f'*[{i}]<a id=e{i} target=_blank>element {i} text />' for i in range(274)]
        raw = '\n'.join(lines)
        result = simplify_elements(raw)
        assert len(result) < 10000

    def test_max_size_control(self):
        big_elements = '\n'.join(f'*[{i}] a: item{i} text here' for i in range(1500))
        data = {
            'status': 'success',
            'url': 'https://www.jd.com/',
            'title': '京东',
            'elements': big_elements,
            'tabSummary': '| TabID | Title |\n| 1 | 京东 |',
            'currentTabId': 1,
        }
        result = fit_response(data, budget=27000)
        total = len(json.dumps(result, ensure_ascii=False))
        assert total < 30000, f'response {total} chars exceeds 30K'
        assert result['tabSummary'] == data['tabSummary']
        assert result['currentTabId'] == 1
        assert '已折叠' in result.get('elements', '') or '折叠' in str(result)

    def test_small_response_not_truncated(self):
        data = {
            'status': 'success',
            'url': 'https://example.com',
            'title': 'Test',
            'elements': '[0] a: 首页\n[1] button: 搜索',
            'tabSummary': '| TabID | Title |\n| 1 | Test |',
            'currentTabId': 1,
        }
        result = fit_response(data, budget=27000)
        assert result['elements'] == data['elements']
        assert result.get('elementsFile') is None

    def test_elementsfile_in_response(self):
        big_elements = '\n'.join(f'*[{i}] a: item{i}' for i in range(1500))
        data = {
            'status': 'success',
            'url': 'https://jd.com',
            'title': 'JD',
            'elements': big_elements,
            'tabSummary': 'tabs',
            'currentTabId': 1,
        }
        result = fit_response(data, budget=27000)
        assert 'elementsFile' in result
        assert result['elementsFile'] is not None

    def test_truncation_path_json_under_30k(self):
        """强制触发截断路径，断言 json.dumps < 30000"""
        big_elements = '\n'.join(f'*[{i}]<a>item {i} detailed text here />' for i in range(1500))
        data = {
            'status': 'success',
            'url': 'https://www.jd.com/',
            'title': '京东商城',
            'elements': big_elements,
            'tabSummary': '| TabID | Title | URL |',
            'currentTabId': 1,
        }
        result = fit_response(data, budget=27000)
        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) < 30000, f'json.dumps = {len(serialized)} chars'
        assert '已折叠' in result['elements']


class TestWriteFullElements:
    def test_writes_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', str(tmp_path))
        path = write_full_elements('[0]<a>test</a>', tag='test_state')
        assert path is not None
        assert 'test_state' in path
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0

    def test_returns_none_on_error(self, monkeypatch):
        monkeypatch.setattr('niu_browser_server.simplify.MD_DIR', '/nonexistent/path')
        result = write_full_elements('[0]<a>test</a>')
        assert result is None
