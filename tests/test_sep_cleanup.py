"""Tests for <SEP> separator cleanup in entity descriptions.

LightRAG merges multi-source entity descriptions using <SEP> as separator.
All display/injection endpoints must clean <SEP> so it never appears raw
in prompts or frontend UI.
"""


class TestCleanSep:
    """Test the _clean_sep helper function."""

    def test_no_sep_returns_unchanged(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "这是一个普通描述，没有分隔符"
        assert _clean_sep(desc) == desc

    def test_single_sep_replaced_with_space(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "描述A<SEP>描述B"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        assert "描述A 描述B" == result

    def test_multiple_sep_replaced(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "A<SEP>B<SEP>C<SEP>D"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        assert "A B C D" == result

    def test_empty_string(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        assert _clean_sep("") == ""

    def test_none_returns_empty(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        assert _clean_sep(None) == ""

    def test_sep_at_start_and_end(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "<SEP>描述<SEP>"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        assert result == " 描述 "

    def test_sep_with_surrounding_spaces(self):
        from niu_api.internal.lightrag_adapter import _clean_sep
        desc = "描述A <SEP> 描述B"
        result = _clean_sep(desc)
        assert "<SEP>" not in result
        # _clean_sep replaces "<SEP>" with " ", surrounding spaces preserved
        assert "描述A   描述B" == result


class TestKgApiFormatDescription:
    """Test that kg_api._format_description cleans <SEP> for non-brainregion types."""

    def test_person_description_sep_cleaned(self):
        from niu_api.kg_api import _format_description
        desc = "李磊是银行员工<SEP>李磊是技术专家"
        result = _format_description("person", desc)
        assert "<SEP>" not in result

    def test_concept_description_sep_cleaned(self):
        from niu_api.kg_api import _format_description
        desc = "概念A<SEP>概念B"
        result = _format_description("concept", desc)
        assert "<SEP>" not in result

    def test_brainregion_still_parsed(self):
        """brainregion type should still go through special parsing, not just space-replace."""
        from niu_api.kg_api import _format_description
        # brainregion with <SEP> triggers _parse_description path
        desc = "summary内容<SEP>brain_meta_shrink_count:2"
        result = _format_description("brainregion", desc)
        # Should not contain raw <SEP> either
        assert "<SEP>" not in result
        assert "brain_meta_" not in result  # brain_meta metadata should be stripped
        assert "summary内容" in result  # summary content should be preserved

