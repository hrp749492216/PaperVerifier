"""Unit tests for paperverifier.utils.json_parser."""

from __future__ import annotations

import pytest

from paperverifier.utils.json_parser import JSONParseError, parse_llm_json


class TestParseValidJSON:
    """parse_llm_json should handle well-formed JSON arrays."""

    def test_valid_json_array(self) -> None:
        raw = '[{"title": "Finding 1", "severity": "major"}]'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["title"] == "Finding 1"

    def test_valid_json_object_wrapped_when_expect_array(self) -> None:
        raw = '{"title": "Finding 1"}'
        result = parse_llm_json(raw, expect_array=True)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_valid_json_object_unwrapped_when_no_expect_array(self) -> None:
        raw = '{"title": "Finding 1"}'
        result = parse_llm_json(raw, expect_array=False)
        assert isinstance(result, dict)
        assert result["title"] == "Finding 1"


class TestCodeFenceStripping:
    """parse_llm_json should strip markdown code fences."""

    def test_json_with_code_fences(self) -> None:
        raw = '```json\n[{"key": "value"}]\n```'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert result[0]["key"] == "value"

    def test_json_with_bare_code_fences(self) -> None:
        raw = '```\n[{"key": "value"}]\n```'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert result[0]["key"] == "value"

    def test_json_with_surrounding_text_and_fences(self) -> None:
        raw = 'Here are the results:\n```json\n[{"a": 1}]\n```\nDone!'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert result[0]["a"] == 1


class TestTruncatedJSON:
    """parse_llm_json should repair truncated JSON with unclosed brackets."""

    def test_truncated_json_unclosed_bracket(self) -> None:
        raw = '[{"title": "Finding 1", "severity": "major"}, {"title": "Finding 2"'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert result[0]["title"] == "Finding 1"

    def test_truncated_json_with_trailing_comma(self) -> None:
        raw = '[{"title": "A"}, {"title": "B"},'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert len(result) == 2


class TestJSON5TrailingCommas:
    """parse_llm_json should handle json5-style trailing commas."""

    def test_trailing_comma_in_array(self) -> None:
        raw = '[{"key": "val1"}, {"key": "val2"},]'
        result = parse_llm_json(raw)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_trailing_comma_in_object(self) -> None:
        raw = '{"key1": "val1", "key2": "val2",}'
        result = parse_llm_json(raw, expect_array=False)
        assert isinstance(result, dict)
        assert result["key1"] == "val1"


class TestEmptyInput:
    """parse_llm_json should raise JSONParseError on empty input."""

    def test_empty_string(self) -> None:
        with pytest.raises(JSONParseError, match="empty"):
            parse_llm_json("")

    def test_none_input(self) -> None:
        with pytest.raises(JSONParseError, match="empty"):
            parse_llm_json(None)  # type: ignore[arg-type]


class TestNonJSONText:
    """parse_llm_json should raise JSONParseError on plain text."""

    def test_plain_text(self) -> None:
        with pytest.raises(JSONParseError):
            parse_llm_json("This is just plain text with no JSON at all.")

    def test_random_characters(self) -> None:
        with pytest.raises(JSONParseError):
            parse_llm_json("abc xyz 123 !@#")
