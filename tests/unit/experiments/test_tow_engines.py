"""
Unit tests for the finder's engine adapters (#737, step 7).

One adapter per answer engine (Perplexity, OpenAI, Gemini, Grok)
plus a fake engine for free end-to-end shakeouts. Each adapter is a
pure build-request / parse-response pair; the network send is a thin
wrapper no unit test touches, and no test makes a live call. The
claim extractor always returns all four claim fields, empty string
when the engine did not provide one, because the typed-evidence gate
checks key presence and an honest empty field becomes an honest
unverifiable verdict downstream.
"""

from typing import Any, Dict

import pytest
from runners.tow_engines import (
    ENGINES,
    FakeEngine,
    GeminiEngine,
    GrokEngine,
    OpenAIEngine,
    PerplexityEngine,
    extract_claim_fields,
)

pytestmark = pytest.mark.unit

LABELED_ANSWER = (
    "Here is what I found.\n"
    "Headline: AI Search Has a Citation Problem\n"
    "Publisher: Columbia Journalism Review\n"
    "Date: March 6, 2025\n"
    "URL: https://www.cjr.org/tow_center/example.php\n"
)


class TestExtractClaimFields:
    """Test suite for the prose-to-claim extractor."""

    def test_labeled_lines_parse_all_four_fields(self) -> None:
        """An engine that labels its answer parses exactly."""
        claim = extract_claim_fields(LABELED_ANSWER, [])

        assert claim["headline"] == "AI Search Has a Citation Problem"
        assert claim["publisher"] == "Columbia Journalism Review"
        assert claim["date"] == "March 6, 2025"
        assert claim["url"] == "https://www.cjr.org/tow_center/example.php"

    def test_citation_fallback_supplies_the_url(self) -> None:
        """Without a labeled URL, the first citation stands in."""
        claim = extract_claim_fields(
            'The quote comes from "A Big Storm" in the Boston Globe.',
            ["https://www.bostonglobe.com/2024/12/11/metro/storm/"],
        )

        assert claim["url"] == "https://www.bostonglobe.com/2024/12/11/metro/storm/"
        assert claim["headline"] == "A Big Storm"

    def test_all_keys_always_present(self) -> None:
        """Missing information yields empty strings, never missing keys."""
        claim = extract_claim_fields("I could not find this quote.", [])

        assert set(claim) == {"headline", "publisher", "date", "url"}
        assert claim["url"] == ""


class TestPerplexityAdapter:
    """Test suite for the Perplexity adapter."""

    def _fixture(self) -> Dict[str, Any]:
        return {
            "choices": [{"message": {"content": LABELED_ANSWER}}],
            "citations": ["https://www.cjr.org/tow_center/example.php"],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80},
            "model": "sonar",
        }

    def test_parse_response(self) -> None:
        """Content, citations, and usage all land on the answer."""
        answer = PerplexityEngine().parse_response(self._fixture())

        assert answer.url == "https://www.cjr.org/tow_center/example.php"
        assert answer.publisher == "Columbia Journalism Review"
        assert LABELED_ANSWER.strip() in answer.raw
        assert answer.prompt_tokens == 120
        assert answer.cost_usd > 0

    def test_build_request_carries_query_and_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The request body holds the study query; the header the key."""
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pk-test")
        request = PerplexityEngine().build_request("THE QUERY")

        assert "THE QUERY" in str(request.body)
        assert request.headers["Authorization"] == "Bearer pk-test"


class TestOpenAIAdapter:
    """Test suite for the OpenAI Responses adapter."""

    def test_parse_response(self) -> None:
        """Message text and url citations parse from the output list."""
        data = {
            "output": [
                {"type": "web_search_call", "id": "ws_1"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": LABELED_ANSWER,
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://www.cjr.org/tow_center/example.php",
                                }
                            ],
                        }
                    ],
                },
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4o-mini",
        }
        answer = OpenAIEngine().parse_response(data)

        assert answer.headline == "AI Search Has a Citation Problem"
        assert answer.url == "https://www.cjr.org/tow_center/example.php"
        assert answer.completion_tokens == 50


class TestGeminiAdapter:
    """Test suite for the Gemini grounding adapter."""

    def test_parse_response(self) -> None:
        """Candidate text and grounding URIs parse."""
        data = {
            "candidates": [
                {
                    "content": {"parts": [{"text": LABELED_ANSWER}]},
                    "groundingMetadata": {
                        "groundingChunks": [
                            {
                                "web": {
                                    "uri": "https://www.cjr.org/tow_center/example.php"
                                }
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 90,
                "candidatesTokenCount": 60,
            },
            "modelVersion": "gemini-2.0-flash",
        }
        answer = GeminiEngine().parse_response(data)

        assert answer.publisher == "Columbia Journalism Review"
        assert answer.url == "https://www.cjr.org/tow_center/example.php"
        assert answer.prompt_tokens == 90


class TestGrokAdapter:
    """Test suite for the Grok search adapter."""

    def test_parse_response(self) -> None:
        """Choice content and citations parse."""
        data = {
            "choices": [{"message": {"content": LABELED_ANSWER}}],
            "citations": ["https://www.cjr.org/tow_center/example.php"],
            "usage": {"prompt_tokens": 70, "completion_tokens": 40},
            "model": "grok-3",
        }
        answer = GrokEngine().parse_response(data)

        assert answer.headline == "AI Search Has a Citation Problem"
        assert answer.url == "https://www.cjr.org/tow_center/example.php"


class TestFakeEngine:
    """Test suite for the free shakeout engine."""

    def test_default_claim_exercises_the_declined_path(self) -> None:
        """The default fake claim carries a URL that will not verify."""
        answer = FakeEngine().query("any query")

        assert answer.url.startswith("https://")
        assert answer.cost_usd == 0.0
        assert answer.raw != ""

    def test_custom_claim_is_returned_verbatim(self) -> None:
        """A supplied claim drives the verified path in shakeouts."""
        claim = {
            "headline": "H",
            "publisher": "P",
            "date": "2024-12-11",
            "url": "https://example.org/x",
        }
        answer = FakeEngine(claim=claim).query("any query")

        assert answer.headline == "H"
        assert answer.url == "https://example.org/x"

    def test_registry_names_all_engines(self) -> None:
        """The registry is the single source of engine names."""
        assert set(ENGINES) == {"perplexity", "openai", "gemini", "grok", "fake"}
