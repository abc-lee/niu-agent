import pytest
from agent.output_validator import validate_references


class TestValidateReferences:
    def test_no_references_passes(self):
        """纯自然语言文本无引用，直接通过"""
        result = validate_references("你好，这是一段普通文字")
        assert result.is_valid is True
        assert result.errors == []

    def test_valid_image_reference_passes(self, tmp_path):
        """图片引用路径存在，通过"""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")
        content = f"![刘永辉]({img})"
        result = validate_references(content)
        assert result.is_valid is True

    def test_invalid_image_reference_fails(self):
        """图片引用路径不存在，失败"""
        content = "![刘永辉](/nonexistent/path/photo.jpg)"
        result = validate_references(content)
        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "图片" in result.errors[0].kind
        assert "/nonexistent/path/photo.jpg" in result.errors[0].path

    def test_url_image_reference_fails(self):
        """图片引用是 URL 而非本地路径，失败"""
        content = "![刘永辉](https://example.com/photo.jpg)"
        result = validate_references(content)
        assert result.is_valid is False
        assert "URL" in result.errors[0].reason

    def test_valid_file_reference_passes(self, tmp_path):
        """文件链接路径存在，通过"""
        doc = tmp_path / "报告.pdf"
        doc.write_bytes(b"%PDF-1.4")
        content = f"[报告.pdf]({doc})"
        result = validate_references(content)
        assert result.is_valid is True

    def test_invalid_file_reference_fails(self):
        """文件链接路径不存在，失败"""
        content = "[报告.pdf](/nonexistent/报告.pdf)"
        result = validate_references(content)
        assert result.is_valid is False
        assert "文件" in result.errors[0].kind

    def test_multiple_errors(self):
        """多个引用错误全部收集"""
        content = "![a](/bad1.jpg) 和 [b.pdf](/bad2.pdf) 和 ![c](/bad3.png)"
        result = validate_references(content)
        assert result.is_valid is False
        assert len(result.errors) == 3

    def test_mixed_valid_and_invalid(self, tmp_path):
        """混合有效和无效引用，只报告无效的"""
        img = tmp_path / "good.jpg"
        img.write_bytes(b"\xff\xd8")
        content = f"![good]({img}) 和 ![bad](/bad.jpg)"
        result = validate_references(content)
        assert result.is_valid is False
        assert len(result.errors) == 1

    def test_file_protocol_stripped(self, tmp_path):
        """file:/// 前缀应被剥离后验证"""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        content = f"![photo](file:///{img})"
        result = validate_references(content)
        assert result.is_valid is True

    def test_format_feedback_message(self):
        """验证错误消息格式包含正确指导"""
        content = "![刘永辉](https://example.com/photo.jpg)"
        result = validate_references(content)
        feedback = result.format_feedback()
        assert "chat-with-file-processor" in feedback
        assert "本地绝对路径" in feedback

    def test_data_uri_image_skipped(self):
        """data: URI 图片不应被验证为本地路径"""
        content = "![chart](data:image/png;base64,iVBORw0KGgo=)"
        result = validate_references(content)
        assert result.is_valid is True
        assert result.errors == []

    def test_empty_path_skipped(self):
        """空路径的链接不应触发验证"""
        content = "[点击下载]()"
        result = validate_references(content)
        assert result.is_valid is True
        assert result.errors == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
