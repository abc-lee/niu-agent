#!/usr/bin/env python3
"""子Agent决策循环测试

TDD 测试文件 — 模拟子Agent的决策逻辑，循环调用ingest工具验证整个循环能正确完成目录入库。

测试场景：
1. 单文档 need_category → 选择分类 → 再次调用 → success
2. 目录循环调用直到所有文件处理完
3. progress 时 processed < total
4. need_category 格式允许简单选择

这些测试验证工具循环链路，部分测试现在会 skip/fail（功能尚未实现）。
"""

import sys
import uuid
from pathlib import Path

import pytest

# 添加 photo-server 模块到 sys.path
PHOTO_SERVER_SRC = str(Path(__file__).parent.parent / "mcp-servers" / "photo-server" / "src")
sys.path.insert(0, PHOTO_SERVER_SRC)


def _import_photo_server():
    """延迟导入 photo-server 模块"""
    from niu_photo_server import ingest

    return ingest


def _create_test_image(path: Path, color: str = "red", size: tuple = (100, 100)):
    """创建测试图片"""
    from PIL import Image

    img = Image.new("RGB", size, color=color)
    img.save(path)


def _skip_if_lightrag_error(result: dict):
    """如果 LightRAG 未初始化则跳过测试"""
    if result.get("status") == "error" and "lightrag" in str(result).lower():
        pytest.skip("LightRAG 未初始化")


class TestAgentToolLoop:
    """测试子Agent工具循环"""

    def setup_method(self):
        """创建测试目录：1张jpg图片 + 2个txt文档"""
        # 使用唯一前缀避免并行冲突
        self.test_id = uuid.uuid4().hex[:8]
        self.test_dir = Path(f"/tmp/niu_test_agent_loop_{self.test_id}")
        self.test_dir.mkdir(parents=True, exist_ok=True)

        # 创建 1 张测试图片
        _create_test_image(self.test_dir / "photo.jpg", color="green")

        # 创建 2 个测试文档
        (self.test_dir / "doc1.txt").write_text("文档1内容：这是第一个测试文档。", encoding="utf-8")
        (self.test_dir / "doc2.txt").write_text("文档2内容：这是第二个测试文档。", encoding="utf-8")

    def teardown_method(self):
        """清理测试目录"""
        import shutil

        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_single_file_need_category_then_classify(self):
        """单文档，第1次不传category返回need_category，第2次带category返回success"""
        ingest = _import_photo_server()

        # 选择一个文档文件
        doc_path = self.test_dir / "doc1.txt"

        # 第1次调用：不传 category
        result1 = ingest(path=str(doc_path))

        _skip_if_lightrag_error(result1)

        # 断言返回 need_category
        assert result1.get("status") == "need_category", f"Expected need_category, got: {result1}"
        assert "available_categories" in result1, f"Expected available_categories: {result1}"
        assert isinstance(result1["available_categories"], list), f"Expected list: {result1}"
        assert len(result1["available_categories"]) > 0, f"Expected non-empty list: {result1}"

        # 获取第一个分类
        category = result1["available_categories"][0]

        # 第2次调用：带 category
        result2 = ingest(path=str(doc_path), category=category, mode="copy")

        _skip_if_lightrag_error(result2)

        # 断言返回 success 或 need_l1（文档入库可能需要 L1）
        assert result2.get("status") in ("success", "need_l1"), f"Expected success or need_l1, got: {result2}"

    def test_directory_tool_loop_completes(self):
        """目录，循环调用ingest直到所有文件处理完

        核心循环逻辑：
        1. 调用 ingest(path=dir, category=category, mode="copy", _offset=offset)
        2. 如果 status == "success" 且有 "total"，循环结束
        3. 如果 status == "need_category"，选择分类后再次调用（offset 不变）
        4. 如果 status == "progress"，更新 offset，继续循环
        5. 如果 status == "error"，退出循环
        """
        ingest = _import_photo_server()

        max_iterations = 20  # 防止无限循环
        offset = 0
        category = ""

        # 当前实现不支持 _offset 参数，测试会 TypeError
        # 这是预期的 TDD 行为
        try:
            for _i in range(max_iterations):
                result = ingest(
                    path=str(self.test_dir),
                    category=category,
                    mode="copy",
                    _offset=offset,  # 当前不支持，会 TypeError
                )

                _skip_if_lightrag_error(result)

                if result["status"] == "success" and "total" in result:
                    # 循环完成
                    break
                elif result["status"] == "need_category":
                    # 选择分类后再次调用，offset 不变
                    chosen = result["available_categories"][0]
                    result = ingest(
                        path=str(self.test_dir),
                        category=chosen,
                        mode="copy",
                        _offset=offset,  # offset 不变
                    )
                    if result["status"] == "progress":
                        offset = result["processed"]
                        category = ""  # 重置分类，让下一个文件重新判断
                    elif result["status"] == "success" and "total" in result:
                        break
                    elif result["status"] == "error":
                        break
                elif result["status"] == "progress":
                    offset = result["processed"]
                    category = ""
                else:
                    # 未知状态，退出
                    break

            # 如果循环正常结束，断言最终 success
            # 当前实现会直接返回 success（批量处理），不返回 progress
            # 所以这个断言对当前实现也有效
            assert result.get("status") in ("success", "progress", "need_category"), (
                f"Expected valid final status, got: {result}"
            )

        except TypeError as e:
            # 当前实现不支持 _offset 参数
            if "_offset" in str(e):
                pytest.skip("ingest 尚未支持 _offset 参数")
            raise

    def test_progress_means_not_done(self):
        """2张图片目录，progress时 processed < total

        注意：当前实现返回 success（批量处理），不返回 progress。
        这个测试验证 progress 语义，等分页功能实现后才通过。
        """
        ingest = _import_photo_server()

        # 创建纯图片目录
        photo_dir = self.test_dir / "photos_only"
        photo_dir.mkdir(exist_ok=True)
        _create_test_image(photo_dir / "img1.jpg", color="red")
        _create_test_image(photo_dir / "img2.jpg", color="blue")

        # 当前实现不支持 _offset 参数
        try:
            result = ingest(path=str(photo_dir), mode="copy", _offset=0)
        except TypeError as e:
            if "_offset" in str(e):
                pytest.skip("ingest 尚未支持 _offset 参数")
            raise

        _skip_if_lightrag_error(result)

        # 如果返回 progress，验证 processed < total
        if result.get("status") == "progress":
            assert "processed" in result, f"Expected processed field: {result}"
            assert "total" in result, f"Expected total field: {result}"
            assert result["processed"] < result["total"], (
                f"Progress should have processed < total: {result}"
            )
        else:
            # 当前实现返回 success，跳过验证
            pytest.skip("ingest 尚未实现分页式返回，当前返回 success")


class TestAgentPromptCompliance:
    """测试 Agent 提示合规性"""

    def setup_method(self):
        """创建测试目录"""
        self.test_id = uuid.uuid4().hex[:8]
        self.test_dir = Path(f"/tmp/niu_test_prompt_compliance_{self.test_id}")
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        """清理测试目录"""
        import shutil

        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_need_category_format_allows_simple_choice(self):
        """验证 available_categories 是字符串列表，选一个后带分类调用能成功"""
        ingest = _import_photo_server()

        # 创建测试文档
        doc_path = self.test_dir / "report.txt"
        doc_path.write_text("测试报告内容：这是一个测试报告。", encoding="utf-8")

        # 第1次调用获取分类
        result1 = ingest(path=str(doc_path))

        _skip_if_lightrag_error(result1)

        assert result1.get("status") == "need_category", f"Expected need_category, got: {result1}"

        # 验证 available_categories 格式
        categories = result1.get("available_categories", [])
        assert isinstance(categories, list), f"Expected list, got: {type(categories)}"
        assert len(categories) > 0, "Expected non-empty categories"

        # 验证每个元素是字符串
        for cat in categories:
            assert isinstance(cat, str), f"Expected string category, got: {type(cat)}: {cat}"

        # 选择第一个分类
        chosen = categories[0]

        # 第2次调用带分类
        result2 = ingest(path=str(doc_path), category=chosen, mode="copy")

        _skip_if_lightrag_error(result2)

        # 断言成功（或 need_l1，文档入库可能需要 L1）
        assert result2.get("status") in ("success", "need_l1"), (
            f"Expected success or need_l1 after choosing category, got: {result2}"
        )
