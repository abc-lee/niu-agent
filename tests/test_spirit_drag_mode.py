"""Test: spirit drag mode fix — resources injection into system_prompt"""
import pytest


def _build_resource_lines(resources):
    """Simulate the injection logic from runner.py — with defensive filtering."""
    if not resources:
        return []
    valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
    if not valid_resources:
        return []
    resource_lines = []
    for r in valid_resources:
        path = r.get("path", "")
        mode = r.get("mode", "copy")
        if mode == "reference":
            resource_lines.append(
                f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用"
            )
        elif mode == "move":
            resource_lines.append(
                f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录"
            )
    return resource_lines


class TestResourcesInjection:
    """Test that resources with mode info are correctly injected into system_prompt."""

    def test_reference_mode_injection(self):
        lines = _build_resource_lines([{"path": "/Users/test/doc.pdf", "mode": "reference"}])
        assert len(lines) == 1
        assert "mode=reference" in lines[0]
        assert "不要拷贝" in lines[0]
        assert "/Users/test/doc.pdf" in lines[0]

    def test_move_mode_injection(self):
        lines = _build_resource_lines([{"path": "/Users/test/file.txt", "mode": "move"}])
        assert len(lines) == 1
        assert "mode=move" in lines[0]
        assert "移动到存储目录" in lines[0]

    def test_copy_mode_no_injection(self):
        """Copy mode is default behavior — no extra instruction generated."""
        lines = _build_resource_lines([{"path": "/Users/test/file.txt", "mode": "copy"}])
        assert len(lines) == 0

    def test_mixed_modes_injection(self):
        """Only non-copy modes generate instructions."""
        lines = _build_resource_lines([
            {"path": "/Users/test/ref.pdf", "mode": "reference"},
            {"path": "/Users/test/move.txt", "mode": "move"},
            {"path": "/Users/test/copy.doc", "mode": "copy"},
        ])
        assert len(lines) == 2
        assert "mode=reference" in lines[0]
        assert "mode=move" in lines[1]

    def test_empty_resources_no_injection(self):
        lines = _build_resource_lines([])
        assert len(lines) == 0

    def test_none_resources_no_injection(self):
        lines = _build_resource_lines(None)
        assert len(lines) == 0

    def test_malformed_resources_filtered(self):
        """Malformed entries (missing path/mode, not dict) are silently filtered."""
        lines = _build_resource_lines([
            {"path": "/Users/test/ref.pdf", "mode": "reference"},
            {"path": "/missing-mode"},
            {"mode": "move"},
            "not_a_dict",
            None,
        ])
        assert len(lines) == 1
        assert "mode=reference" in lines[0]


class TestFullSystemPromptInjection:
    """Test the complete system_prompt assembly with resources."""

    def test_resources_appended_after_skills(self):
        base_prompt = "你是助手。"
        injection = "\n\n【技能】\n某些技能内容"
        resources = [{"path": "/Users/test/doc.pdf", "mode": "reference"}]

        system_prompt = base_prompt
        if injection:
            system_prompt += injection

        lines = _build_resource_lines(resources)
        if lines:
            system_prompt += (
                "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n"
                + "\n".join(lines)
            )

        assert "【技能】" in system_prompt
        assert "【文件操作模式要求】" in system_prompt
        assert "mode=reference" in system_prompt

    def test_no_resources_no_extra_section(self):
        base_prompt = "你是助手。"
        resources = None

        system_prompt = base_prompt
        lines = _build_resource_lines(resources)
        if lines:
            system_prompt += (
                "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n"
                + "\n".join(lines)
            )

        assert "【文件操作模式要求】" not in system_prompt
        assert system_prompt == base_prompt
