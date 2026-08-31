"""browser-server 集成测试——mock bridge，零网络依赖。

import niu_browser_server 会启动 WSBridge 线程（模块级副作用，pre-existing）——
测试用 mock _ensure_connection 隔离，不触发真实 WebSocket。
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "browser-server", "src"))


def _make_big_elements(n=1500):
    """生成 n 个元素的大 elements 字符串（真实格式）"""
    return '\n'.join(
        f'*[{i}]<a id=item{i} target=_blank>item{i} text here />'
        for i in range(n)
    )


def _mock_bridge_return(elements):
    """构造 mock bridge 返回"""
    return {
        'success': True,
        'data': {
            'url': 'https://www.jd.com/',
            'title': '京东',
            'elements': elements,
            'pageInfo': {},
            'tabSummary': '| TabID | Title | URL |\n|-------|-------|-----|\n| 1 | 京东 | https://jd.com |',
            'currentTabId': 1,
        }
    }


class TestInteractSimplify:
    def test_get_state_big_page_simplified(self):
        """大页面 get_state → 精简 + tabSummary 保留 + 全 json<30K"""
        mock_result = _mock_bridge_return(_make_big_elements(1500))

        with patch('niu_browser_server._ensure_connection') as mc:
            mc.return_value = MagicMock(send_command=MagicMock(return_value=mock_result))
            from niu_browser_server import browser_interact
            result = browser_interact(action='get_state')

        assert result['status'] == 'success'
        # 核心验收：全 dict JSON < 30K
        assert len(json.dumps(result, ensure_ascii=False)) < 30000
        # tabSummary 完整
        assert 'TabID' in result['tabSummary']
        assert result['currentTabId'] == 1
        # elementsFile 在返回 dict 中
        assert 'elementsFile' in result

    def test_navigate_big_page_simplified(self):
        """大页面 navigate → 同样精简（不只是 get_state）"""
        mock_result = _mock_bridge_return(_make_big_elements(1500))

        with patch('niu_browser_server._ensure_connection') as mc:
            mc.return_value = MagicMock(send_command=MagicMock(return_value=mock_result))
            from niu_browser_server import browser_navigate
            result = browser_navigate(url='https://www.jd.com/')

        assert result['status'] == 'success'
        assert len(json.dumps(result, ensure_ascii=False)) < 30000
        assert 'elementsFile' in result

    def test_small_page_not_simplified(self):
        """小页面不精简不截断"""
        small = '*[0]<a>首页 />\n*[1]<button>搜索 />'
        mock_result = _mock_bridge_return(small)

        with patch('niu_browser_server._ensure_connection') as mc:
            mc.return_value = MagicMock(send_command=MagicMock(return_value=mock_result))
            from niu_browser_server import browser_interact
            result = browser_interact(action='get_state')

        assert result['elements'] == small  # 原样

    def test_click_no_elements(self):
        """click 不返回 elements，不受影响"""
        mock_result = {
            'success': True,
            'data': {'url': 'https://example.com', 'title': 'T', 'elements': '', 'pageInfo': {}}
        }

        with patch('niu_browser_server._ensure_connection') as mc:
            mc.return_value = MagicMock(send_command=MagicMock(return_value=mock_result))
            from niu_browser_server import browser_interact
            result = browser_interact(action='click', index=0)

        assert result['status'] == 'success'

    def test_new_tab_big_page_simplified(self):
        """new_tab 大页面同样精简"""
        mock_result = _mock_bridge_return(_make_big_elements(1500))

        with patch('niu_browser_server._ensure_connection') as mc:
            mc.return_value = MagicMock(send_command=MagicMock(return_value=mock_result))
            from niu_browser_server import browser_new_tab
            result = browser_new_tab(url='https://www.jd.com/')

        assert result['status'] == 'success'
        assert len(json.dumps(result, ensure_ascii=False)) < 30000

    def test_switch_tab_big_page_simplified(self):
        """switch_tab 大页面同样精简"""
        mock_result = _mock_bridge_return(_make_big_elements(1500))

        with patch('niu_browser_server._ensure_connection') as mc:
            mc.return_value = MagicMock(send_command=MagicMock(return_value=mock_result))
            from niu_browser_server import browser_switch_tab
            result = browser_switch_tab(tab_id=1)

        assert result['status'] == 'success'
        assert len(json.dumps(result, ensure_ascii=False)) < 30000
