"""search_persons 查询失败语义测试（方案 v1.6 §6，T1–T6）。

纪律：真跑 search_persons / call_tool（含真 SQL 分支），只打桩依赖
（get_connection 抛错，或 NIU_DB_PATH/WORKSPACE_PATH 指向 tmp 真库）；
不加载 InsightFace/ONNX。autouse fixture 全套隔离（先例
tests/test_ingest_paging.py:44-64 的 _reset_photo_module_state + photo_env）。

不变量 I1：裸列表只属于成功路径；任何失败路径永不返回 list。
I4：query 在成功与失败两面都原样回显。
"""

import asyncio
import json
import os
import sqlite3
import sys

import pytest

# photo-server 模块路径（先例 tests/test_browser_elements_simplify.py:16）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src"))

def _reset_photo_module_state():
    """复位模块级缓存（先例 tests/test_ingest_paging.py:44-51）：连接缓存不复位会
    指向真实 photos.db，粘性 _db_path_failed 会让后续用例持续报错，
    会话缓存不复位会串状态。"""
    import niu_photo_server

    niu_photo_server._conn = None
    niu_photo_server._db_path_failed = False
    niu_photo_server._ingest_sessions.clear()


@pytest.fixture(autouse=True)
def photo_env(tmp_path, monkeypatch):
    """临时工作区/照片库路径重定向 + 模块状态复位，覆盖本文件全部用例。

    真库用例（T2/T3/T5）依赖 NIU_DB_PATH 绝对路径优先级（get_db_path 第 1 级），
    get_connection 会在 tmp 下自动建库建表；打桩用例（T1/T4/T6）再 monkeypatch
    get_connection 抛错。"""
    import niu_photo_server

    _reset_photo_module_state()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("NIU_DB_PATH", str(tmp_path / "photos.db"))
    monkeypatch.setenv("WORKSPACE_PATH", str(workspace))

    yield {"workspace": workspace, "db_file": tmp_path / "photos.db"}
    _reset_photo_module_state()


class TestFailureEnvelope:
    """T1 失败⇒报错 / T6 失败不得返回 list：打桩 get_connection 抛库锁错误。"""

    def setup_method(self):
        import niu_photo_server as ps

        self.ps = ps
        self._real_get_connection = ps.get_connection
        ps.get_connection = lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        )

    def teardown_method(self):
        self.ps.get_connection = self._real_get_connection

    def test_t1_failure_returns_error_envelope(self):
        """T1：失败 ⇒ status:error + error_code + query 回显 + 澄清 message。"""
        ret = self.ps.search_persons(query="张三")
        assert isinstance(ret, dict), ret
        assert ret["status"] == "error"
        assert ret["error_code"] == "PERSON_SEARCH_FAILED"
        assert ret["query"] == "张三"
        assert "database is locked" in ret["message"]
        assert "不代表" in ret["message"]

    def test_t6_failure_never_returns_list(self):
        """T6：失败不得返回 list（I1：[] 语义由 status:success 独占）。"""
        ret = self.ps.search_persons(query="张三")
        assert not isinstance(ret, list)


class TestRealDb:
    """T2 真空库 / T3 有结果 / T5 转义分支：tmp 真库、真 SQL。"""

    def test_t2_empty_db_success_empty_results(self):
        """T2：真库 0 行 ⇒ status:success + results==[] + count==0（真结论，不是错误）。"""
        import niu_photo_server as ps

        ret = ps.search_persons(query="不存在的人")
        assert isinstance(ret, dict), ret
        assert ret["status"] == "success"
        assert ret["query"] == "不存在的人"
        assert ret["results"] == []
        assert ret["count"] == 0

    def test_t3_hit_returns_full_fields(self):
        """T3：tmp 真库插 1 行 person ⇒ success + 1 条，字段 id/name/auto_label/photo_count 齐。"""
        import niu_photo_server as ps

        conn = ps.get_connection()
        conn.execute(
            "INSERT INTO persons (id, name, auto_label, photo_count) VALUES (?, ?, ?, ?)",
            ("p-0001", "张三丰", "A-01", 3),
        )
        conn.commit()

        ret = ps.search_persons(query="张三")
        assert ret["status"] == "success"
        assert len(ret["results"]) == 1
        row = ret["results"][0]
        for key in ("id", "name", "auto_label", "photo_count"):
            assert key in row, f"字段 {key} 缺失: {row}"
        assert row["id"] == "p-0001"
        assert row["name"] == "张三丰"
        assert row["auto_label"] == "A-01"
        assert row["photo_count"] == 3
        assert ret["count"] == 1

    def test_t5_escape_branch_no_throw(self):
        """T5：query 含 LIKE 通配符 % ⇒ 走转义分支，不抛，返回 success 信封。"""
        import niu_photo_server as ps

        ret = ps.search_persons(query="100%")
        assert ret["status"] == "success"
        assert ret["results"] == []
        assert ret["count"] == 0


class TestStdioPassthrough:
    """T4 stdio 透传：call_tool 不得再盖 success 章，error_code 必须原样透出。"""

    def test_t4_call_tool_passes_error_envelope(self, monkeypatch):
        import niu_photo_server as ps

        monkeypatch.setattr(
            ps, "get_connection",
            lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            ),
        )

        resp = asyncio.run(
            ps.call_tool("search_persons", {"query": "张三"})
        )
        # call_tool 返回 list[TextContent]，先解包（先例 tests/test_daily_tools.py:266-271）
        payload = json.loads(resp[0].text)
        assert isinstance(payload, dict), payload
        assert payload["status"] == "error"
        # 只断言 status 不够：函数改回 return [] 时 call_tool 兜底也是 status:error 但丢 error_code
        assert payload["error_code"] == "PERSON_SEARCH_FAILED"
        assert payload["query"] == "张三"
