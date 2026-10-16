"""search_persons / calculate_content_similarity 失败语义测试。

search_persons：方案 v1.6 §6（T1–T6）；calculate_content_similarity：
sibling closeout 2026-10-16 §2.1（S1–S5，信封化：失败不得折算 0.0）。
纪律：真跑函数本体（search_persons 含真 SQL 分支；calculate_content_similarity
真读 tmp 文件），只打桩依赖（get_connection 抛错 / 向量服务 / 冲突配置）；
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


class TestContentSimilarityFailure:
    """S1–S4：calculate_content_similarity 失败语义（信封化，closeout §2.1）。

    打桩依赖：call_embedding_service（向量服务）、read_file_content（读文件抛错）；
    文件用 tmp_path 真实文件。
    """

    def test_missing_file_returns_error_envelope(self, tmp_path):
        """S1：文件不存在 ⇒ status:error + FILE_NOT_FOUND（不得折算 0.0）。"""
        import niu_photo_server as ps

        f1 = tmp_path / "exists.txt"
        f1.write_text("内容", encoding="utf-8")
        f2 = tmp_path / "not_exist.txt"

        ret = ps.calculate_content_similarity(str(f1), str(f2))
        assert isinstance(ret, dict), ret
        assert ret["status"] == "error"
        assert ret["error_code"] == "FILE_NOT_FOUND"
        assert "读文件失败" in ret["message"]
        assert "similarity" not in ret, "失败不得携带 similarity:0.0 冒充毫无相似"

    def test_read_failure_returns_error_envelope(self, tmp_path, monkeypatch):
        """S1b：读文件抛错（txt 分支 open 失败）⇒ status:error + FILE_READ_FAILED。"""
        import niu_photo_server as ps

        f1 = tmp_path / "a.txt"
        f1.write_text("内容", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_text("内容2", encoding="utf-8")

        def _raise(path, max_chars=20000):
            raise PermissionError(f"[Errno 13] Permission denied: {path}")

        monkeypatch.setattr(ps, "read_file_content", _raise)
        ret = ps.calculate_content_similarity(str(f1), str(f2))
        assert ret["status"] == "error"
        assert ret["error_code"] == "FILE_READ_FAILED"
        assert "Permission denied" in ret["message"]

    def test_truly_empty_file_is_success_zero(self, tmp_path, monkeypatch):
        """S2：真空文件 ⇒ status:success 且 similarity == 0.0（真结论，不得算失败）。"""
        import niu_photo_server as ps

        # 服务打桩成可用：若空文本被误判为失败，此桩永远不会被调用
        monkeypatch.setattr(
            ps, "call_embedding_service", lambda ep, data: {"similarity": 0.99}
        )
        f1 = tmp_path / "empty.txt"
        f1.write_text("", encoding="utf-8")
        f2 = tmp_path / "also_empty.txt"
        f2.write_text("", encoding="utf-8")

        ret = ps.calculate_content_similarity(str(f1), str(f2))
        assert ret["status"] == "success"
        assert ret["similarity"] == 0.0
        assert ret["file1"] == str(f1)
        assert ret["file2"] == str(f2)

    def test_embedding_service_down_is_error_not_zero(self, tmp_path, monkeypatch):
        """S3：向量服务不可用（返回 None）⇒ status:error + EMBEDDING_SERVICE_UNAVAILABLE，
        绝不返回 0.0 冒充"毫无相似"。"""
        import niu_photo_server as ps

        monkeypatch.setattr(ps, "call_embedding_service", lambda ep, data: None)
        f1 = tmp_path / "a.txt"
        f1.write_text("文档 A 的内容", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_text("文档 B 的内容", encoding="utf-8")

        ret = ps.calculate_content_similarity(str(f1), str(f2))
        assert ret["status"] == "error"
        assert ret["error_code"] == "EMBEDDING_SERVICE_UNAVAILABLE"
        assert "similarity" not in ret, "失败信封不得携带 similarity 键"

    def test_normal_path_passthrough_matches_old_formula(self, tmp_path, monkeypatch):
        """S4：正常路径数值与旧公式一致——旧实现即原样返回服务侧 result["similarity"]，
        故断言信封 similarity 与打桩返回值逐位相等，且送给服务的文本就是文件内容。"""
        import niu_photo_server as ps

        sent = {}

        def _fake_service(endpoint, data):
            sent.update(data)
            return {"similarity": 0.87}

        monkeypatch.setattr(ps, "call_embedding_service", _fake_service)
        f1 = tmp_path / "a.txt"
        f1.write_text("文档 A 的内容", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_text("文档 B 的内容", encoding="utf-8")

        ret = ps.calculate_content_similarity(str(f1), str(f2))
        assert ret["status"] == "success"
        assert ret["similarity"] == 0.87  # 与旧实现返回的 result["similarity"] 完全一致
        assert ret["file1"] == str(f1)
        assert ret["file2"] == str(f2)
        assert sent["text1"] == "文档 A 的内容"  # 读取/截断链路未变
        assert sent["text2"] == "文档 B 的内容"


class TestHandleConflictCallerSurface:
    """S5 调用面：handle_conflict 必须解开信封——错误信封不得被当 0.0 静默吞掉，
    成功信封数值语义不变（>阈值 ⇒ 版本管理）。"""

    def _make_doc_pair(self, tmp_path):
        target = tmp_path / "doc.txt"
        target.write_text("目标文档内容 A", encoding="utf-8")
        source = tmp_path / "new.txt"
        source.write_text("全新内容 B", encoding="utf-8")
        return target, source

    def _stub_prefs_and_similarity(self, monkeypatch, ps, similarity_result):
        monkeypatch.setattr(ps, "get_preferences", lambda: {
            "storage": {"conflict": {"document": {"similarity_threshold": 0.85}}},
        })
        monkeypatch.setattr(
            ps, "calculate_content_similarity",
            lambda f1, f2: similarity_result,
        )

    def test_error_envelope_not_silently_zero(self, tmp_path, monkeypatch):
        """S5a：错误信封 ⇒ 按不相似处理（改名）并如实告警，函数不得因 dict 返回体崩溃。"""
        import niu_photo_server as ps

        target, source = self._make_doc_pair(tmp_path)
        self._stub_prefs_and_similarity(monkeypatch, ps, {
            "status": "error",
            "error_code": "EMBEDDING_SERVICE_UNAVAILABLE",
            "message": "向量服务不可用",
        })

        final_path, action = ps.handle_conflict(target, source)
        assert action == "renamed"
        assert final_path.endswith("_1.txt")
        assert target.exists(), "改名路径：原文件保留原名"

    def test_success_above_threshold_versions(self, tmp_path, monkeypatch):
        """S5b：成功且 similarity > 阈值 ⇒ 版本管理（原文件移到 _v1_，新内容接管原文件名）。"""
        import niu_photo_server as ps

        target, source = self._make_doc_pair(tmp_path)
        self._stub_prefs_and_similarity(monkeypatch, ps, {
            "status": "success", "file1": str(target), "file2": str(source),
            "similarity": 0.95,
        })

        final_path, action = ps.handle_conflict(target, source)
        assert action == "versioned"
        assert final_path == str(target)
        assert not target.exists(), "原文件已被移到版本名"
        versioned = [p for p in target.parent.iterdir()
                     if p.name.startswith(target.stem + "_v")]
        assert len(versioned) == 1
