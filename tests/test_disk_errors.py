"""Tests for disk_errors.py — Error templates and formatting."""

import pytest

from niu_api.internal.disk_errors import DiskErrors, FuzzyMatcher
from niu_api.internal.disk_config import ToolConfig, ArgConfig


@pytest.fixture
def errors():
    return DiskErrors()


@pytest.fixture
def sample_tool():
    return ToolConfig(
        name="explore_node",
        summary="探索实体邻居",
        description="从实体出发探索N层邻居。",
        args=[
            ArgConfig(name="entity_id", type="string", description="实体ID", position=1, required=True),
            ArgConfig(name="depth", type="integer", description="遍历深度1-5", default=2, flag="depth",
                      constraints={"minimum": 1, "maximum": 5}),
            ArgConfig(name="min_confidence", type="number", description="最小置信度", default=0.0,
                      flag="min-confidence", constraints={"minimum": 0.0, "maximum": 1.0}),
            ArgConfig(name="direction", type="string", description="方向", default="both",
                      flag="direction", enum=["both", "outgoing", "incoming"]),
        ],
        examples=["/kg/explore_node Einstein"],
    )


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

class TestFuzzyMatcher:
    def test_close_match_suggestion(self):
        result = FuzzyMatcher.suggest("depth", ["depth", "direction", "min-confidence"])
        assert result == "depth"

    def test_typo_suggestion(self):
        result = FuzzyMatcher.suggest("depht", ["depth", "direction", "min-confidence"])
        assert result == "depth"

    def test_no_match_too_far(self):
        result = FuzzyMatcher.suggest("xyz", ["depth", "direction", "min-confidence"])
        assert result is None

    def test_no_flags_available(self):
        result = FuzzyMatcher.suggest("anything", [])
        assert result is None

    def test_empty_input(self):
        result = FuzzyMatcher.suggest("", ["depth"])
        assert result is None


# ---------------------------------------------------------------------------
# Error E1: Unknown command
# ---------------------------------------------------------------------------

class TestE1:
    def test_unknown_command(self, errors):
        msg = errors.unknown_command("rm")
        assert "rm" in msg
        assert "ls" in msg
        assert "cat" in msg


# ---------------------------------------------------------------------------
# Error E2: Path not found
# ---------------------------------------------------------------------------

class TestE2:
    def test_path_not_found(self, errors):
        msg = errors.path_not_found("/nonexist", ["kg", "memory", "photos"])
        assert "/nonexist" in msg
        assert "kg" in msg
        assert "memory" in msg


# ---------------------------------------------------------------------------
# Error E3: Tool not found
# ---------------------------------------------------------------------------

class TestE3:
    def test_tool_not_found_no_truncation(self, errors):
        tools = ["explore_node", "query_graph", "find_path", "hub_entities",
                 "get_related_entities", "get_related_concepts"]
        msg = errors.tool_not_found("/kg", "nonexist", tools)
        assert "nonexist" in msg
        # All tools must be listed, no truncation
        for t in tools:
            assert t in msg


# ---------------------------------------------------------------------------
# Error E4: Is a directory
# ---------------------------------------------------------------------------

class TestE4:
    def test_is_directory(self, errors):
        msg = errors.is_directory("/kg")
        assert "/kg" in msg
        assert "directory" in msg.lower()


# ---------------------------------------------------------------------------
# Error E5: Missing required arg (self-contained)
# ---------------------------------------------------------------------------

class TestE5:
    def test_missing_required_arg(self, errors, sample_tool):
        msg = errors.missing_required_arg("/kg", sample_tool, "entity_id")
        assert "entity_id" in msg
        assert "/kg/explore_node" in msg
        # Must be self-contained: include ALL args
        assert "--depth" in msg
        assert "--min-confidence" in msg
        assert "--direction" in msg

    def test_e5_includes_example(self, errors, sample_tool):
        msg = errors.missing_required_arg("/kg", sample_tool, "entity_id")
        assert "/kg/explore_node Einstein" in msg


# ---------------------------------------------------------------------------
# Error E6: Type mismatch
# ---------------------------------------------------------------------------

class TestE6:
    def test_type_mismatch(self, errors):
        msg = errors.type_mismatch("/kg/explore_node", "depth", "integer", "abc")
        assert "depth" in msg
        assert "integer" in msg
        assert "abc" in msg


# ---------------------------------------------------------------------------
# Error E7: Unknown flag with fuzzy match
# ---------------------------------------------------------------------------

class TestE7:
    def test_unknown_flag_with_suggestion(self, errors, sample_tool):
        msg = errors.unknown_flag("/kg", sample_tool, "layers",
                                  ["depth", "direction", "min-confidence"])
        assert "layers" in msg
        assert "depth" in msg

    def test_unknown_flag_no_suggestion(self, errors, sample_tool):
        msg = errors.unknown_flag("/kg", sample_tool, "zzzzz",
                                  ["depth", "direction", "min-confidence"])
        assert "zzzzz" in msg
        assert "unknown" in msg.lower()


# ---------------------------------------------------------------------------
# Error E8: Enum value error
# ---------------------------------------------------------------------------

class TestE8:
    def test_enum_error(self, errors):
        msg = errors.enum_error("/kg/explore_node", "direction", "sideways",
                                ["both", "outgoing", "incoming"])
        assert "sideways" in msg
        assert "both" in msg
        assert "outgoing" in msg


# ---------------------------------------------------------------------------
# Error E9: Constraint out of range
# ---------------------------------------------------------------------------

class TestE9:
    def test_out_of_range(self, errors):
        msg = errors.out_of_range("/kg/explore_node", "depth", 10, minimum=1, maximum=5)
        assert "10" in msg
        assert "1-5" in msg


# ---------------------------------------------------------------------------
# Error E10: Too many positional args
# ---------------------------------------------------------------------------

class TestE10:
    def test_too_many_args(self, errors):
        msg = errors.too_many_args("/kg/explore_node", expected=1, actual=2)
        assert "1" in msg
        assert "2" in msg


# ---------------------------------------------------------------------------
# Error E11: Empty command
# ---------------------------------------------------------------------------

class TestE11:
    def test_empty_command(self, errors):
        msg = errors.empty_command()
        assert "ls" in msg
        assert "cat" in msg


# ---------------------------------------------------------------------------
# Error E12: MCP execution failure
# ---------------------------------------------------------------------------

class TestE12:
    def test_execution_failure(self, errors):
        msg = errors.execution_failure("/kg/explore_node", "Entity not found")
        assert "/kg/explore_node" in msg
        assert "Entity not found" in msg


# ---------------------------------------------------------------------------
# Error E13: cd not supported
# ---------------------------------------------------------------------------

class TestE13:
    def test_cd_not_supported(self, errors):
        msg = errors.cd_not_supported()
        assert "cd" in msg
        assert "absolute" in msg.lower()


# ---------------------------------------------------------------------------
# Error E14: Flag missing value
# ---------------------------------------------------------------------------

class TestE14:
    def test_flag_missing_value(self, errors):
        msg = errors.flag_missing_value("/kg/explore_node", "depth")
        assert "depth" in msg


# ---------------------------------------------------------------------------
# Error E15: Execute directory
# ---------------------------------------------------------------------------

class TestE15:
    def test_execute_directory(self, errors):
        msg = errors.execute_directory("/kg")
        assert "/kg" in msg
        assert "directory" in msg.lower()


# ---------------------------------------------------------------------------
# Error E16: Shell syntax
# ---------------------------------------------------------------------------

class TestE16:
    def test_pipe_error(self, errors):
        msg = errors.shell_syntax("pipe")
        assert "pipe" in msg.lower()

    def test_chaining_error(self, errors):
        msg = errors.shell_syntax("chaining")
        assert "chaining" in msg.lower()


# ---------------------------------------------------------------------------
# Error E17: Repeated errors escalation
# ---------------------------------------------------------------------------

class TestE17:
    def test_escalation_includes_full_usage(self, errors, sample_tool):
        msg = errors.repeated_errors("/kg", sample_tool)
        assert "/kg/explore_node" in msg
        assert "entity_id" in msg
        assert "--depth" in msg
        assert "repeated" in msg.lower()

    def test_escalation_includes_example(self, errors, sample_tool):
        msg = errors.repeated_errors("/kg", sample_tool)
        assert "/kg/explore_node Einstein" in msg


# ---------------------------------------------------------------------------
# Error E18: Mutually exclusive
# ---------------------------------------------------------------------------

class TestE18:
    def test_mutually_exclusive(self, errors):
        msg = errors.mutually_exclusive("/kg/explore_node", "id", "query",
                                        ["id", "query", "filter"])
        assert "id" in msg
        assert "query" in msg
        assert "mutually exclusive" in msg.lower()


# ---------------------------------------------------------------------------
# First error tip
# ---------------------------------------------------------------------------

class TestFirstErrorTip:
    def test_first_error_includes_tip(self, errors):
        msg = errors.with_first_error_tip("some error message", "/kg/explore_node",
                                          first_error=True)
        assert "Tip" in msg
        assert "cat /kg/explore_node" in msg

    def test_second_error_no_tip(self, errors):
        msg = errors.with_first_error_tip("some error message", "/kg/explore_node",
                                          first_error=False)
        assert "Tip" not in msg
