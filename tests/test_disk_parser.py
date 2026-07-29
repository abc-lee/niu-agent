"""Tests for disk_parser.py — Command tokenization and parsing."""

import pytest

from niu_api.internal.disk_parser import DiskParser


@pytest.fixture
def parser():
    return DiskParser()


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_simple_command(self, parser):
        tokens, _ = parser.tokenize("ls /kg")
        assert tokens == ["ls", "/kg"]

    def test_quoted_string(self, parser):
        tokens, _ = parser.tokenize('/kg/explore_node "John Smith"')
        assert tokens == ["/kg/explore_node", "John Smith"]

    def test_single_quotes(self, parser):
        tokens, _ = parser.tokenize("/memory/remember 'hello world'")
        assert tokens == ["/memory/remember", "hello world"]

    def test_flag_with_value(self, parser):
        tokens, _ = parser.tokenize("/kg/explore_node Einstein --depth 3")
        assert tokens == ["/kg/explore_node", "Einstein", "--depth", "3"]

    def test_flag_equals_value(self, parser):
        # --flag=value stays as single token; _parse_args splits it
        tokens, _ = parser.tokenize("/tool --name=value")
        assert tokens == ["/tool", "--name=value"]

    def test_empty_string(self, parser):
        tokens, _ = parser.tokenize("")
        assert tokens == []

    def test_whitespace_only(self, parser):
        tokens, _ = parser.tokenize("   ")
        assert tokens == []

    def test_double_dash_terminator(self, parser):
        tokens, term_at = parser.tokenize("/kg/explore_node -- --important-flag")
        assert tokens == ["/kg/explore_node", "--important-flag"]
        assert term_at == 1  # -- was after first token


# ---------------------------------------------------------------------------
# Shell special syntax detection
# ---------------------------------------------------------------------------

class TestShellSyntaxDetection:
    def test_pipe_rejected(self, parser):
        result = parser.parse("/kg/explore_node Einstein | grep friend")
        assert result.action == "SHELL_SYNTAX"
        assert "pipe" in result.error_msg.lower()

    def test_and_chaining_rejected(self, parser):
        result = parser.parse("/kg/explore_node Einstein && /memory/remember x")
        assert result.action == "SHELL_SYNTAX"
        assert "&&" in result.error_msg

    def test_semicolon_rejected(self, parser):
        result = parser.parse("/kg/explore_node Einstein ; /memory/remember x")
        assert result.action == "SHELL_SYNTAX"
        assert ";" in result.error_msg

    def test_redirect_rejected(self, parser):
        result = parser.parse("/kg/explore_node Einstein > output.txt")
        assert result.action == "SHELL_SYNTAX"
        assert "redirect" in result.error_msg.lower()

    def test_wildcard_rejected(self, parser):
        result = parser.parse("ls /kg/explore*")
        assert result.action == "SHELL_SYNTAX"
        assert "wildcard" in result.error_msg.lower()

    def test_variable_rejected(self, parser):
        result = parser.parse("/kg/explore_node $ENTITY")
        assert result.action == "SHELL_SYNTAX"
        assert "variable" in result.error_msg.lower()

    def test_background_rejected(self, parser):
        result = parser.parse("/kg/explore_node Einstein &")
        assert result.action == "SHELL_SYNTAX"
        assert "background" in result.error_msg.lower()

    def test_pipe_inside_quotes_allowed(self, parser):
        """Pipe inside a quoted string should NOT be rejected."""
        result = parser.parse('/tool "hello | world"')
        assert result.action != "SHELL_SYNTAX"


# ---------------------------------------------------------------------------
# Action recognition
# ---------------------------------------------------------------------------

class TestActionRecognition:
    def test_ls_action(self, parser):
        result = parser.parse("ls /kg")
        assert result.action == "LIST"
        assert result.path == "/kg"

    def test_ls_root(self, parser):
        result = parser.parse("ls")
        assert result.action == "LIST"
        assert result.path == "/"

    def test_cat_action(self, parser):
        result = parser.parse("cat /kg/explore_node")
        assert result.action == "READ"
        assert result.path == "/kg/explore_node"

    def test_help_action(self, parser):
        result = parser.parse("help")
        assert result.action == "HELP"

    def test_execute_action(self, parser):
        result = parser.parse("/kg/explore_node Einstein --depth 3")
        assert result.action == "EXECUTE"
        assert result.dir_name == "kg"
        assert result.tool_name == "explore_node"

    def test_unknown_command(self, parser):
        result = parser.parse("rm /some/path")
        assert result.action == "UNKNOWN"
        assert result.raw == "rm /some/path"

    def test_empty_command(self, parser):
        result = parser.parse("")
        assert result.action == "EMPTY"

    def test_cd_command(self, parser):
        result = parser.parse("cd /kg")
        assert result.action == "UNKNOWN"


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------

class TestPathParsing:
    def test_root_path(self, parser):
        result = parser.parse("ls /")
        assert result.path == "/"

    def test_directory_path(self, parser):
        result = parser.parse("ls /kg")
        assert result.path == "/kg"

    def test_tool_path(self, parser):
        result = parser.parse("/kg/explore_node Einstein")
        assert result.dir_name == "kg"
        assert result.tool_name == "explore_node"

    def test_directory_only_path_for_execute(self, parser):
        result = parser.parse("/kg")
        assert result.action == "EXECUTE"
        assert result.dir_name == "kg"
        assert result.tool_name is None

    def test_ls_with_all_flag(self, parser):
        result = parser.parse("ls --all /kg")
        assert result.action == "LIST"
        assert result.path == "/kg"
        assert result.flags.get("all") is True


# ---------------------------------------------------------------------------
# Argument parsing (EXECUTE action)
# ---------------------------------------------------------------------------

class TestArgumentParsing:
    def test_positional_args(self, parser):
        result = parser.parse("/kg/explore_node Einstein")
        assert result.positional_args == ["Einstein"]

    def test_flag_args(self, parser):
        result = parser.parse("/kg/explore_node Einstein --depth 3")
        assert result.positional_args == ["Einstein"]
        assert result.flag_args == {"depth": "3"}

    def test_multiple_flags(self, parser):
        result = parser.parse("/kg/explore_node Einstein --depth 3 --direction outgoing")
        assert result.flag_args == {"depth": "3", "direction": "outgoing"}

    def test_boolean_flag(self, parser):
        result = parser.parse("/tool --verbose")
        assert result.flag_args == {"verbose": True}

    def test_no_args(self, parser):
        result = parser.parse("/kg/graph_stats")
        assert result.positional_args == []
        assert result.flag_args == {}

    def test_kebab_flag(self, parser):
        result = parser.parse("/kg/explore_node Einstein --min-confidence 0.5")
        assert result.flag_args == {"min-confidence": "0.5"}

    def test_flag_with_equals(self, parser):
        result = parser.parse("/tool --depth=3")
        assert result.flag_args == {"depth": "3"}

    def test_double_dash_values(self, parser):
        result = parser.parse("/kg/explore_node -- --important-flag")
        assert result.positional_args == ["--important-flag"]
