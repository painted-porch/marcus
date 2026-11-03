"""
JSON Parser Utility.

Handles parsing JSON that may be wrapped in markdown code blocks or
have other formatting issues. This is common when working with LLM
responses that return formatted JSON.
"""

import json
import logging
import re
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


def _balanced_structure_end(text: str, start: int) -> Optional[int]:
    """Return the index just past the JSON structure opening at ``start``.

    Walks the text tracking nesting of both bracket kinds while ignoring
    anything inside string literals (and escaped quotes within them), so
    a ``]`` or ``}`` in a ``reasoning`` field does not end the structure
    early.

    Parameters
    ----------
    text : str
        The full response.
    start : int
        Index of the opening ``{`` or ``[``.

    Returns
    -------
    Optional[int]
        End index (exclusive), or None when the structure never closes.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json_from_response(response: str) -> str:
    """
    Extract JSON content from a response with markdown or other formatting.

    Parameters
    ----------
    response : str
        The raw response string that may contain JSON

    Returns
    -------
    str
        The extracted JSON string

    Raises
    ------
    ValueError
        If no valid JSON structure can be found
    """
    if not response:
        raise ValueError("Empty response provided")

    # First, try the response as-is
    response = response.strip()

    # Check if wrapped in markdown code blocks
    if response.startswith("```"):
        logger.debug("Detected markdown code block wrapper")

        # Try to extract JSON from ```json ... ``` or ``` ... ```
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", response, re.DOTALL)
        if json_match:
            extracted = json_match.group(1).strip()
            logger.debug(f"Extracted {len(extracted)} chars from markdown block")
            return extracted
        else:
            # Fallback: try to find JSON structure between backticks
            lines = response.split("\n")
            if len(lines) > 2:
                # Remove first and last line if they contain backticks
                if lines[0].strip().startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                extracted = "\n".join(lines).strip()
                logger.debug(
                    f"Extracted {len(extracted)} chars using line-based approach"
                )
                return extracted

    # Check if the response has JSON structure (starts with { or [)
    # A reply that begins with the structure may still keep talking after it
    # (``[...]\n\nNote: ...``), which ``json.loads`` rejects as "Extra data".
    # Trim to the balanced structure in that case too (Simon 65e11785).
    json_start = response.find("{")
    array_start = response.find("[")
    if json_start == -1 and array_start == -1:
        raise ValueError("No JSON structure found in response")

    if json_start == -1:
        start = array_start
    elif array_start == -1:
        start = json_start
    else:
        start = min(json_start, array_start)

    end = _balanced_structure_end(response, start)
    if end is None:
        raise ValueError("Unmatched brackets in JSON structure")

    extracted = response[start:end]
    logger.debug(f"Extracted JSON structure: {len(extracted)} chars")
    return extracted


def parse_json_response(
    response: str, default: Optional[Any] = None
) -> Union[Dict[str, Any], list[Any], Any]:
    """
    Parse a JSON response with markdown or other formatting.

    Parameters
    ----------
    response : str
        The raw response string that may contain JSON
    default : Optional[Any], optional
        Default value to return if parsing fails (if None, raises exception)

    Returns
    -------
    Union[Dict[str, Any], list[Any], Any]
        The parsed JSON object

    Raises
    ------
    json.JSONDecodeError
        If JSON parsing fails and no default is provided
    ValueError
        If no valid JSON can be extracted and no default is provided
    """
    try:
        # Extract JSON from the response
        json_str = extract_json_from_response(response)

        # Parse the JSON
        result = json.loads(json_str)
        logger.debug(
            f"Successfully parsed JSON: {type(result).__name__} with "
            f"{len(str(result))} chars"
        )
        return result

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse JSON response: {e}")
        if default is not None:
            logger.debug(f"Returning default value: {type(default).__name__}")
            return default
        raise


def clean_json_response(response: str) -> str:
    """
    Clean a JSON response by removing common formatting issues.

    Parameters
    ----------
    response : str
        The raw response string

    Returns
    -------
    str
        Cleaned JSON string
    """
    # Remove markdown code blocks
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = extract_json_from_response(cleaned)

    # Remove common prefixes that LLMs might add
    prefixes_to_remove = [
        "Here is the JSON response:",
        "Here's the JSON:",
        "JSON response:",
        "Response:",
        "Output:",
    ]

    for prefix in prefixes_to_remove:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix) :].strip()

    # Remove trailing explanations (look for JSON end and cut there)
    if cleaned.startswith("{"):
        # Find the last matching }
        bracket_count = 0
        last_bracket = -1
        for i, char in enumerate(cleaned):
            if char == "{":
                bracket_count += 1
            elif char == "}":
                bracket_count -= 1
                if bracket_count == 0:
                    last_bracket = i
        if last_bracket != -1:
            cleaned = cleaned[: last_bracket + 1]

    return cleaned


def validate_json_structure(
    data: Any, required_fields: Optional[list[str]] = None
) -> bool:
    """
    Validate that parsed JSON has expected structure.

    Parameters
    ----------
    data : Any
        The parsed JSON data
    required_fields : Optional[list[str]], optional
        List of required field names (for dict validation)

    Returns
    -------
    bool
        True if structure is valid, False otherwise
    """
    if required_fields and isinstance(data, dict):
        return all(field in data for field in required_fields)
    return True


# Convenience function for AI response parsing
def parse_ai_json_response(
    response: str, required_fields: Optional[list[str]] = None
) -> Dict[str, Any]:
    """
    Parse JSON from AI responses with validation.

    Parameters
    ----------
    response : str
        The AI response that should contain JSON
    required_fields : Optional[list[str]], optional
        Optional list of required fields to validate

    Returns
    -------
    Dict[str, Any]
        Parsed and validated JSON dictionary

    Raises
    ------
    ValueError
        If response doesn't contain valid JSON or required fields are
        missing
    """
    # Clean and parse the response
    cleaned = clean_json_response(response)
    parsed = parse_json_response(cleaned)

    # Ensure we have a dictionary for validation
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")

    # Validate structure if required fields specified
    if required_fields:
        missing_fields = [field for field in required_fields if field not in parsed]
        if missing_fields:
            raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

    return parsed
