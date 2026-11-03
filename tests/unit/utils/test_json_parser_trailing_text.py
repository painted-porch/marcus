"""
Unit tests for extracting JSON that is followed or wrapped by prose.

The hybrid dependency inferer parsed model replies with a bare
``json.loads``; a reply that started with the JSON array and then added a
sentence failed with ``Extra data`` and the whole batch was discarded
(Simon 65e11785). The shared extractor also returned a reply unchanged
whenever it began with a bracket, so it had the same blind spot. These
pin the balanced-structure trim for arrays and objects in every position.
"""

import json

import pytest

from src.utils.json_parser import extract_json_from_response, parse_json_response

pytestmark = pytest.mark.unit

ARRAY = [{"task1_id": "1", "task2_id": "2", "dependency_direction": "none"}]


class TestLeadingStructureWithTrailingText:
    """A reply that starts with JSON and keeps talking still yields the JSON."""

    def test_array_then_prose(self) -> None:
        """The exact shape that failed: array first, commentary after."""
        reply = json.dumps(ARRAY, indent=2) + "\n\nNote: these are my best guesses."
        assert json.loads(extract_json_from_response(reply)) == ARRAY

    def test_object_then_prose(self) -> None:
        """Same for an object."""
        reply = json.dumps({"a": 1}) + " I hope this helps!"
        assert json.loads(extract_json_from_response(reply)) == {"a": 1}

    def test_array_inside_fence_then_prose(self) -> None:
        """A fenced array followed by a sentence."""
        reply = "```json\n" + json.dumps(ARRAY) + "\n```\nLet me know if you need more."
        assert parse_json_response(reply) == ARRAY

    def test_prose_then_array(self) -> None:
        """Leading prose was already handled; it still is."""
        reply = "Here are the dependencies:\n" + json.dumps(ARRAY)
        assert parse_json_response(reply) == ARRAY

    def test_bare_array_unchanged(self) -> None:
        """The happy path is untouched."""
        assert parse_json_response(json.dumps(ARRAY)) == ARRAY

    def test_brackets_inside_strings_do_not_confuse_the_trim(self) -> None:
        """A bracket character inside a string value is not a structural bracket."""
        payload = {"reasoning": "closes ] early?", "x": [1, 2]}
        reply = json.dumps(payload) + " trailing"
        assert json.loads(extract_json_from_response(reply)) == payload
