"""
Tests for basic tool renaming.

Testing the new function signatures and behaviors:
- file_read -> read_file(file_path, offset=1, limit=500)
- file_write -> write_file(file_path, content, mode="overwrite")
- file_patch -> edit_file(file_path, old_string, new_string, replace_all=False)
- new: grep_search(pattern, path=".", include="")
"""


# ---------------------------------------------------------------------------
# Import: use new names only
# ---------------------------------------------------------------------------

import os

from agent.handler import edit_file, grep_search, read_file, write_file

# ===================================================================
# read_file tests
# ===================================================================

class TestReadFile:
    """Tests for read_file(file_path, offset=1, limit=2000)"""

    def test_basic_read_returns_linenumber_format(self, tmp_path):
        """Reading a file returns content with 'line_number|content' format."""
        f = tmp_path / "sample.txt"
        f.write_text("hello\nworld\nfoo\n", encoding="utf-8")

        result = read_file(str(f))

        # Each line should have format "N|content"
        lines = result.strip().split("\n")
        # Find lines that contain actual file content (skip header lines like [FILE]...)
        content_lines = [line for line in lines if "|" in line and not line.startswith("[")]
        assert len(content_lines) == 3
        # Check format: number|text
        for line in content_lines:
            parts = line.split("|", 1)
            assert parts[0].strip().isdigit(), f"Line number not found: {line}"

    def test_offset_parameter(self, tmp_path):
        """offset parameter starts reading from the specified line (1-based)."""
        f = tmp_path / "multi.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        # New API: offset=3 means start from line 3
        result = read_file(str(f), offset=3)

        # Should NOT contain line1 or line2
        assert "line1" not in result
        assert "line2" not in result
        # Should contain line3
        assert "line3" in result

    def test_limit_parameter(self, tmp_path):
        """limit parameter restricts the number of lines read."""
        f = tmp_path / "long.txt"
        lines = [f"line{i}" for i in range(1, 101)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # New API: limit=5 means read at most 5 lines
        result = read_file(str(f), limit=5)

        # Count content lines (those with | separator)
        content_lines = [line for line in result.split("\n") if "|" in line and not line.startswith("[")]
        assert len(content_lines) <= 5

    def test_limit_hard_cap_at_500(self, tmp_path):
        """When limit > 500, it should be automatically capped to 500."""
        f = tmp_path / "huge.txt"
        lines = [f"line{i}" for i in range(1, 601)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Request 99999 lines, but hard cap is 500
        result = read_file(str(f), limit=99999)

        content_lines = [line for line in result.split("\n") if "|" in line and not line.startswith("[")]
        assert len(content_lines) <= 500, f"Expected at most 500 lines, got {len(content_lines)}"

    def test_file_not_found(self):
        """Reading a non-existent file returns an error message."""
        result = read_file("/nonexistent/path/file.txt")

        assert "error" in result.lower() or "not found" in result.lower()

    def test_directory_path_returns_error(self, tmp_path):
        """Passing a directory path instead of a file returns an error."""
        result = read_file(str(tmp_path))

        assert "error" in result.lower() or "directory" in result.lower()

    def test_backward_compat_old_param_names(self, tmp_path):
        """Old parameter names (path, start, count) should still work via do_read adapter.

        This tests that the Handler.do_read method maps old param names to new ones,
        so existing tool calls using 'path'/'start'/'count' don't break.
        """
        from agent.handler import Handler

        f = tmp_path / "compat.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")

        handler = Handler.__new__(Handler)
        # Simulate a tool call using OLD parameter names
        result = handler.do_read(
            {"path": str(f), "start": 2, "count": 2},
            response=None,
        )

        # Should read lines 2-3 (beta, gamma), not fail
        text = result.output if hasattr(result, "output") else str(result)
        assert "beta" in text
        assert "gamma" in text
        assert "alpha" not in text

    def test_do_read_negative_offset_passthrough(self, tmp_path):
        """do_read passes negative offset through to read_file (tail via tool layer)."""
        from agent.handler import Handler

        f = tmp_path / "tail_compat.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")

        handler = Handler.__new__(Handler)
        result = handler.do_read({"path": str(f), "offset": -2}, response=None)

        text = result.output if hasattr(result, "output") else str(result)
        assert "gamma" in text
        assert "delta" in text
        assert "alpha" not in text

    def test_offset_zero_normalized_to_one(self, tmp_path):
        """offset=0 should be normalized to 1."""
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = read_file(str(f), offset=0)
        assert "line1" in result

    def test_negative_offset_returns_last_lines(self, tmp_path):
        """Negative offset reads the last |offset| lines (tail semantics)."""
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        result = read_file(str(f), offset=-3)

        assert "line1" not in result
        assert "line2" not in result
        assert "line3" in result
        assert "line4" in result
        assert "line5" in result

    def test_offset_exceeds_total_lines(self, tmp_path):
        """offset beyond file length should return clear message."""
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\n", encoding="utf-8")

        result = read_file(str(f), offset=100)
        assert "exceeds total lines" in result

    def test_negative_offset_exceeds_total_lines_returns_all(self, tmp_path):
        """offset=-N with N > total lines returns the whole file, never an error."""
        f = tmp_path / "fifty.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n", encoding="utf-8")

        result = read_file(str(f), offset=-300)

        content_lines = [line for line in result.split("\n") if "|" in line and not line.startswith("[")]
        assert len(content_lines) == 50
        assert "line1" in result
        assert "line50" in result
        assert "error" not in result.lower()

    def test_negative_offset_empty_file(self, tmp_path):
        """offset=-N on an empty file returns 'No content', never crashes."""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")

        result = read_file(str(f), offset=-10)

        assert "No content" in result
        assert "error" not in result.lower()

    def test_negative_offset_last_single_line(self, tmp_path):
        """offset=-1 returns only the very last line with its real line number."""
        f = tmp_path / "three.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = read_file(str(f), offset=-1)

        content_lines = [line for line in result.split("\n") if "|" in line and not line.startswith("[")]
        assert len(content_lines) == 1
        assert "3|line3" in result

    def test_negative_offset_linenumbers_are_absolute(self, tmp_path):
        """Tail mode reports real file line numbers, not 1-based tail positions."""
        f = tmp_path / "hundred.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 101)) + "\n", encoding="utf-8")

        result = read_file(str(f), offset=-50)

        assert "51|line51" in result
        assert "100|line100" in result
        assert "1|line1" not in result

    def test_negative_offset_with_limit(self, tmp_path):
        """limit still caps returned lines after tail positioning (51-60 of 100)."""
        f = tmp_path / "hundred.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 101)) + "\n", encoding="utf-8")

        result = read_file(str(f), offset=-50, limit=10)

        content_lines = [line for line in result.split("\n") if "|" in line and not line.startswith("[")]
        assert len(content_lines) == 10
        assert "51|line51" in result
        assert "60|line60" in result

    # ---- 页预算分页（2026-08-28 read 工具智能分页）----

    def test_short_lines_full_page_no_marker(self, tmp_path):
        """①短行一次读满：200 行短行全返回、无 Truncated 标记、每行完整。"""
        f = tmp_path / "short.txt"
        lines = [f"line{i}" for i in range(1, 201)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = read_file(str(f))

        content_lines = [l for l in result.split("\n") if "|" in l and not l.startswith("[")]
        assert content_lines == [f"{i}|{t}" for i, t in enumerate(lines, 1)]
        assert "TRUNCATED" not in result
        assert "[Truncated at line" not in result

    def test_budget_stops_page_at_line_boundary(self, tmp_path):
        """②按行截断：2000 字符行×30 → 每页恒 14 行，返回行全完整无 [TRUNCATED]，标记在输出末尾且 offset=15。"""
        f = tmp_path / "wide.txt"
        payload = "x" * 2000
        f.write_text("\n".join([payload] * 30) + "\n", encoding="utf-8")

        result = read_file(str(f))
        out_lines = result.split("\n")
        assert out_lines[0] == "[FILE] Showing 14 lines from line 1 (total 30 lines)"
        content_lines = [l for l in out_lines if "|" in l and not l.startswith("[")]
        assert len(content_lines) == 14
        assert "TRUNCATED" not in result
        for i in range(1, 15):
            assert content_lines[i - 1] == f"{i}|{payload}"
        # 标记在输出末尾且指向 offset=15
        assert out_lines[-1] == "[Truncated at line 14. Use offset=15 to read more.]"

        # 第二页同样恒 14 行（15-28）
        result2 = read_file(str(f), offset=15)
        out_lines2 = result2.split("\n")
        assert out_lines2[0] == "[FILE] Showing 14 lines from line 15 (total 30 lines)"
        content_lines2 = [l for l in out_lines2 if "|" in l and not l.startswith("[")]
        assert len(content_lines2) == 14
        assert "TRUNCATED" not in result2
        assert out_lines2[-1] == "[Truncated at line 28. Use offset=29 to read more.]"

    def test_single_line_over_budget_first_line_fallback(self, tmp_path):
        """③单行超预算：首行 40000 字符+后续 5 短行 → [TRUNCATED] 在场、本页仅此行、标记指向 offset=2；
        另断言：单行文件（唯一行超预算）→无续读标记。"""
        tag = " ... [TRUNCATED]"
        f = tmp_path / "monster.txt"
        big = "y" * 40000
        f.write_text(big + "\n" + "\n".join(f"tail{i}" for i in range(1, 6)) + "\n", encoding="utf-8")

        result = read_file(str(f))
        out_lines = result.split("\n")
        assert out_lines[0] == "[FILE] Showing 1 lines from line 1 (total 6 lines)"
        content_lines = [l for l in out_lines if "|" in l and not l.startswith("[")]
        assert len(content_lines) == 1
        expected_cut = 29000 - len(tag) - len("1|") - 1
        assert content_lines[0] == f"1|{big[:expected_cut]}{tag}"
        assert out_lines[-1] == "[Truncated at line 1. Use offset=2 to read more.]"

        # 单行文件（唯一行超预算）→无续读标记
        g = tmp_path / "single.txt"
        g.write_text("z" * 40000 + "\n", encoding="utf-8")
        result2 = read_file(str(g))
        assert "TRUNCATED" in result2
        assert "[Truncated at line" not in result2

    def test_over_budget_line_mid_page_defers_to_next_page(self, tmp_path):
        """④页中遇超预算行：3 短行+1 行 29000 字符+后续短行 → 首页含前 3 行、标记指向第 4 行；
        offset=4 续读页首行=该超长行（走兜底截断）。"""
        tag = " ... [TRUNCATED]"
        f = tmp_path / "mid.txt"
        big = "w" * 29000
        f.write_text("\n".join(["a", "b", "c", big, "d", "e"]) + "\n", encoding="utf-8")

        result = read_file(str(f))
        out_lines = result.split("\n")
        assert out_lines[0] == "[FILE] Showing 3 lines from line 1 (total 6 lines)"
        content_lines = [l for l in out_lines if "|" in l and not l.startswith("[")]
        assert content_lines == ["1|a", "2|b", "3|c"]
        assert out_lines[-1] == "[Truncated at line 3. Use offset=4 to read more.]"

        result2 = read_file(str(f), offset=4)
        out_lines2 = result2.split("\n")
        content_lines2 = [l for l in out_lines2 if "|" in l and not l.startswith("[")]
        assert len(content_lines2) == 1
        expected_cut = 29000 - len(tag) - len("4|") - 1
        assert content_lines2[0] == f"4|{big[:expected_cut]}{tag}"
        assert out_lines2[-1] == "[Truncated at line 4. Use offset=5 to read more.]"

    def test_budget_invariant_random_lengths(self, tmp_path):
        """⑤预算不变式：随机行长文件（固定种子，行长 1~5000 混合 200 行）→
        每页整体返回 ≤30000 字符、主内容区 ≤29000。"""
        import random

        rng = random.Random(42)
        lines = ["m" * rng.randint(1, 5000) for _ in range(200)]
        f = tmp_path / "rand.txt"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

        offset = 1
        pages = 0
        while True:
            result = read_file(str(f), offset=offset)
            assert len(result) <= 30000, f"page {pages}: total {len(result)} > 30000"
            out_lines = result.split("\n")
            body = out_lines[1:]
            if body and body[-1].startswith("[Truncated at line"):
                body = body[:-1]
            content = "\n".join(body)
            assert len(content) <= 29000, f"page {pages}: content {len(content)} > 29000"
            pages += 1
            if not out_lines[-1].startswith("[Truncated at line"):
                break
            offset = int(out_lines[-1].split("offset=")[1].split(" ")[0])
        assert pages >= 2  # 200 行随机长度必然分页

    def test_tail_offset_with_budget(self, tmp_path):
        """⑥tail 回归：offset=-3 行为与预算组合正确（短行全返回；长行 tail 在行边界停）。"""
        f1 = tmp_path / "tail_short.txt"
        f1.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
        result = read_file(str(f1), offset=-3)
        content_lines = [l for l in result.split("\n") if "|" in l and not l.startswith("[")]
        assert content_lines == ["8|line8", "9|line9", "10|line10"]
        # 末返回行号=总行数 → 无续读标记
        assert "[Truncated at line" not in result

        f2 = tmp_path / "tail_wide.txt"
        wide = [f"line{i}" for i in range(1, 8)] + ["t" * 20000, "u" * 20000, "v" * 20000]
        f2.write_text("\n".join(wide) + "\n", encoding="utf-8")
        result2 = read_file(str(f2), offset=-3)
        out_lines2 = result2.split("\n")
        content_lines2 = [l for l in out_lines2 if "|" in l and not l.startswith("[")]
        assert len(content_lines2) == 1
        assert content_lines2[0] == "8|" + "t" * 20000
        # 下一行将超预算 → 页在行边界停，标记指向 offset=9
        assert out_lines2[-1] == "[Truncated at line 8. Use offset=9 to read more.]"


# ===================================================================
# write_file tests
# ===================================================================

class TestWriteFile:
    """Tests for write_file(file_path, content)"""

    def test_basic_write(self, tmp_path):
        """Writing content to a file succeeds and content can be read back."""
        f = tmp_path / "output.txt"

        result = write_file(str(f), "hello world")

        # Should indicate success
        assert isinstance(result, dict)
        assert result.get("status") == "success"
        # File should contain the written content
        assert f.read_text(encoding="utf-8") == "hello world"

    def test_auto_create_parent_directory(self, tmp_path):
        """If parent directory doesn't exist, it should be created automatically."""
        f = tmp_path / "subdir" / "nested" / "deep.txt"

        result = write_file(str(f), "nested content")

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "nested content"

    def test_overwrite_existing_file(self, tmp_path):
        """Writing to an existing file overwrites its content."""
        f = tmp_path / "existing.txt"
        f.write_text("old content", encoding="utf-8")

        write_file(str(f), "new content")

        assert f.read_text(encoding="utf-8") == "new content"

    def test_append_mode(self, tmp_path):
        """mode='append' adds content to the end of the file."""
        f = tmp_path / "append.txt"
        f.write_text("first line\n", encoding="utf-8")

        result = write_file(str(f), "second line\n", mode="append")

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "first line\nsecond line\n"

    def test_overwrite_mode_default(self, tmp_path):
        """Default mode is 'overwrite', which replaces entire file."""
        f = tmp_path / "default.txt"
        f.write_text("original", encoding="utf-8")

        result = write_file(str(f), "replacement")

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "replacement"

    def test_append_to_nonexistent_file(self, tmp_path):
        """mode='append' on a nonexistent file creates the file (shows 'Written' not 'Appended')."""
        f = tmp_path / "new_append.txt"

        result = write_file(str(f), "first content\n", mode="append")

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "first content\n"
        # Message should say "Written" not "Appended" since file didn't exist
        assert "Written" in result.get("msg", "")


# ===================================================================
# edit_file tests
# ===================================================================

class TestEditFile:
    """Tests for edit_file(file_path, old_string, new_string, replace_all=False)"""

    def test_basic_replacement(self, tmp_path):
        """Replacing a unique string in a file succeeds."""
        f = tmp_path / "edit.txt"
        f.write_text("hello world\nfoo bar\n", encoding="utf-8")

        # New API: old_string / new_string parameter names
        result = edit_file(str(f), old_string="hello world", new_string="hi earth")

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "hi earth\nfoo bar\n"

    def test_old_string_not_found(self, tmp_path):
        """When old_string doesn't exist in file, returns error."""
        f = tmp_path / "edit.txt"
        f.write_text("hello world\n", encoding="utf-8")

        result = edit_file(str(f), old_string="nonexistent", new_string="replacement")

        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert "not found" in result.get("msg", "").lower()

    def test_non_unique_old_string_without_replace_all(self, tmp_path):
        """When old_string matches multiple times and replace_all=False, returns error."""
        f = tmp_path / "dup.txt"
        f.write_text("aaa bbb aaa ccc aaa\n", encoding="utf-8")

        result = edit_file(str(f), old_string="aaa", new_string="zzz", replace_all=False)

        assert isinstance(result, dict)
        assert result.get("status") == "error"
        # Should hint about using replace_all or mention uniqueness
        msg = result.get("msg", "").lower()
        assert "unique" in msg or "replace_all" in msg or "multiple" in msg, \
            "Error message should mention uniqueness or replace_all option"

    def test_replace_all_true(self, tmp_path):
        """With replace_all=True, all occurrences are replaced."""
        f = tmp_path / "multi.txt"
        f.write_text("aaa bbb aaa ccc aaa\n", encoding="utf-8")

        result = edit_file(str(f), old_string="aaa", new_string="zzz", replace_all=True)

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "zzz bbb zzz ccc zzz\n"

    def test_old_string_empty_returns_error(self, tmp_path):
        """When old_string is empty, returns error (prevents accidental wipe)."""
        f = tmp_path / "edit.txt"
        f.write_text("some content\n", encoding="utf-8")

        result = edit_file(str(f), old_string="", new_string="replacement")

        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert "empty" in result.get("msg", "").lower()

    def test_file_not_found(self, tmp_path):
        """Editing a non-existent file returns an error."""
        f = tmp_path / "nonexistent.txt"

        result = edit_file(str(f), old_string="foo", new_string="bar")

        assert isinstance(result, dict)
        assert result.get("status") == "error"

    def test_old_string_equals_new_string(self, tmp_path):
        """When old_string == new_string, returns error (no-op)."""
        f = tmp_path / "same.txt"
        f.write_text("hello world\n", encoding="utf-8")

        result = edit_file(str(f), old_string="hello", new_string="hello")

        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert "identical" in result.get("msg", "").lower()

    def test_delete_by_empty_new_string(self, tmp_path):
        """Replacing with empty new_string effectively deletes old_string."""
        f = tmp_path / "delete.txt"
        f.write_text("keep this\nremove this\nkeep this too\n", encoding="utf-8")

        result = edit_file(str(f), old_string="remove this\n", new_string="")

        assert isinstance(result, dict)
        assert result.get("status") == "success"
        assert f.read_text(encoding="utf-8") == "keep this\nkeep this too\n"


# ===================================================================
# grep_search tests
# ===================================================================

class TestGrepSearch:
    """Tests for grep_search(pattern, path=".", include="")"""

    def test_basic_search_returns_file_line_content(self, tmp_path):
        """Searching in a directory returns 'filepath:lineno:content' format."""
        # Create test files — use lowercase to match case-sensitive search
        f1 = tmp_path / "code.py"
        f1.write_text("def hello():\n    print('world')\n", encoding="utf-8")
        f2 = tmp_path / "readme.md"
        f2.write_text("# hello world\nSome text\n", encoding="utf-8")

        result = grep_search("hello", str(tmp_path))

        # Result should contain file path, line number, and content
        assert "code.py" in result
        # Format should be filepath:lineno:content
        lines = result.strip().split("\n")
        match_lines = [line for line in lines if ":" in line and "code.py" in line]
        if match_lines:
            parts = match_lines[0].split(":")
            assert len(parts) >= 3, f"Expected 'file:line:content' format, got: {match_lines[0]}"

    def test_regex_search(self, tmp_path):
        """grep_search supports regex patterns (case-sensitive)."""
        f = tmp_path / "data.py"
        f.write_text("var_1 = 10\nVar_2 = 20\nconst = 30\n", encoding="utf-8")

        # Case-sensitive: var_\d matches var_1 but NOT Var_2
        result = grep_search(r"var_\d", str(tmp_path))

        assert "var_1" in result
        assert "Var_2" not in result
        assert "const" not in result

    def test_include_parameter_filters_by_glob(self, tmp_path):
        """include parameter filters search to files matching the glob pattern."""
        # Create files with different extensions
        py_file = tmp_path / "script.py"
        py_file.write_text("search_target_here\n", encoding="utf-8")
        js_file = tmp_path / "app.js"
        js_file.write_text("search_target_here\n", encoding="utf-8")
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("search_target_here\n", encoding="utf-8")

        result = grep_search("search_target", str(tmp_path), include="*.py")

        # Should only find the .py file
        assert "script.py" in result
        assert "app.js" not in result
        assert "notes.txt" not in result

    def test_no_matches_returns_info(self, tmp_path):
        """When no matches are found, returns a 'No matches' message."""
        f = tmp_path / "empty.txt"
        f.write_text("nothing relevant here\n", encoding="utf-8")

        result = grep_search("zzzznonexistent", str(tmp_path))

        assert "no match" in result.lower() or "not found" in result.lower() or "0 match" in result.lower(), \
            "Should indicate no matches found"

    def test_partial_read_failure_reported_with_no_match(self, tmp_path):
        """Partial read failure keeps 'No matches' but appends the failure list."""
        good = tmp_path / "good.txt"
        good.write_text("nothing relevant here\n", encoding="utf-8")
        locked = tmp_path / "locked.txt"
        locked.write_text("secret data\n", encoding="utf-8")
        os.chmod(locked, 0)  # no read permission -> OSError on open

        result = grep_search("zzzznonexistent", str(tmp_path))

        # Partial failure: No matches text preserved + failure count/list appended
        assert "no match" in result.lower()
        assert "failed to read" in result
        assert "locked.txt" in result

    def test_all_read_failures_do_not_report_no_match(self, tmp_path):
        """When every file fails to read, report the failure instead of 'No matches'."""
        locked = tmp_path / "locked.txt"
        locked.write_text("secret data\n", encoding="utf-8")
        os.chmod(locked, 0)  # no read permission -> OSError on open

        result = grep_search("zzzznonexistent", str(tmp_path))

        # All-failed branch: explicit failure, no misleading "No matches"
        assert "读取失败" in result
        assert "无法确认匹配" in result
        assert "no match" not in result.lower()

    def test_matches_with_read_failure_appends_failure_note(self, tmp_path):
        """When matches exist and some files fail to read, append the failure note."""
        good = tmp_path / "good.txt"
        good.write_text("needle here\n", encoding="utf-8")
        locked = tmp_path / "locked.txt"
        locked.write_text("needle also here\n", encoding="utf-8")
        os.chmod(locked, 0)  # no read permission -> OSError on open

        result = grep_search("needle", str(tmp_path))

        # Match lines present
        assert "good.txt" in result
        assert "needle here" in result
        # Failure note appended at end of match results (count + truncated path list)
        assert "另有 1 个文件读取失败" in result
        assert "locked.txt" in result

    def test_result_limit_50(self, tmp_path):
        """Search results are limited to at most 50 matches."""
        # Create a file with 60 matching lines
        f = tmp_path / "many.py"
        lines = [f"# match_line_{i}" for i in range(60)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = grep_search("match_line", str(tmp_path))

        # Count result lines (skip header/summary lines)
        result_lines = [line for line in result.strip().split("\n") if line.strip() and ":" in line]
        assert len(result_lines) <= 50, f"Expected at most 50 results, got {len(result_lines)}"

    def test_nonexistent_path_returns_error(self):
        """Searching in a nonexistent path returns an error message."""
        result = grep_search("test", "/nonexistent/path/that/does/not/exist")
        assert "does not exist" in result.lower() or "error" in result.lower()
