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
    ClaudeEngine,
    ClaudeHaikuEngine,
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

    def test_prose_answer_from_the_live_smoke_parses(self) -> None:
        """The real OpenAI prose shape yields all four fields.

        Captured verbatim from the first live smoke call: quoted
        title, 'published on <date>, by <publisher>', markdown link.
        """
        prose = (
            "The quote you provided is from an article titled "
            '"New England rainfall totals from storm as flooding '
            'persists," published on December 11, 2024, by The '
            "Boston Globe. ([bostonglobe.com](https://www.bostonglobe.com"
            "/2024/12/11/metro/new-england-rain-storm-flash-flooding/"
            "?utm_source=openai))\n\n**Citation:**\nMahan, K. (2024, "
            "December 11). New England rainfall totals from storm as "
            "flooding persists. *The Boston Globe*."
        )

        claim = extract_claim_fields(prose, [])

        assert claim["headline"].startswith("New England rainfall totals")
        assert "Boston Globe" in claim["publisher"]
        assert claim["date"] == "December 11, 2024"
        assert "bostonglobe.com/2024/12/11" in claim["url"]


class TestPerplexityAdapter:
    """Test suite for the Perplexity Agent API adapter.

    The live smoke found the legacy chat endpoint retired (403 with a
    migration notice); the Agent API at /v1/responses answers in the
    Responses shape and reports exact cost in usage.cost.
    """

    def _fixture(self) -> Dict[str, Any]:
        return {
            "output": [
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
                }
            ],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 80,
                "cost": {"currency": "USD", "total_cost": 0.00602},
            },
            "model": "perplexity/sonar",
        }

    def test_parse_response(self) -> None:
        """Content, citations, and EXACT provider cost all land."""
        answer = PerplexityEngine().parse_response(self._fixture())

        assert answer.url == "https://www.cjr.org/tow_center/example.php"
        assert answer.publisher == "Columbia Journalism Review"
        assert LABELED_ANSWER.strip() in answer.raw
        assert answer.prompt_tokens == 120
        assert answer.cost_usd == 0.00602

    def test_build_request_carries_query_and_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Agent API request: /v1/responses, namespaced model."""
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pk-test")
        request = PerplexityEngine().build_request("THE QUERY")

        assert request.url == "https://api.perplexity.ai/v1/responses"
        assert request.body["model"] == "perplexity/sonar"
        assert request.body["tools"] == [{"type": "web_search"}]
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

    def test_web_search_fee_is_captured(self) -> None:
        """Each web_search_call item bills $0.01 and is recorded.

        The Responses API returns one ``web_search_call`` item per
        search; OpenAI bills those separately from tokens, so the
        token-only cost silently undercounts a grounded answer.
        """
        data = {
            "output": [
                {"type": "web_search_call", "id": "ws_1"},
                {"type": "web_search_call", "id": "ws_2"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": LABELED_ANSWER,
                            "annotations": [],
                        }
                    ],
                },
            ],
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
            "model": "gpt-4o-mini",
        }
        answer = OpenAIEngine().parse_response(data)

        assert answer.search_requests == 2
        assert answer.search_fee_usd == pytest.approx(0.02)
        assert answer.cost_usd == pytest.approx(
            OpenAIEngine()._cost(1_000_000, 0) + 0.02
        )


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

    def test_grounding_fee_is_captured(self) -> None:
        """A grounded answer bills one grounding request at $0.035.

        Google bills "Grounding with Google Search" per grounded
        prompt, separate from tokens; one grounded call is one request.
        """
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
            "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0},
            "modelVersion": "gemini-3.5-flash",
        }
        answer = GeminiEngine().parse_response(data)

        assert answer.search_requests == 1
        assert answer.search_fee_usd == pytest.approx(0.035)
        assert answer.cost_usd == pytest.approx(0.035)

    def test_ungrounded_answer_has_no_grounding_fee(self) -> None:
        """No grounding metadata means no grounding request billed."""
        data = {
            "candidates": [
                {"content": {"parts": [{"text": "I could not find this excerpt."}]}}
            ],
            "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0},
            "modelVersion": "gemini-3.5-flash",
        }
        answer = GeminiEngine().parse_response(data)

        assert answer.search_requests == 0
        assert answer.search_fee_usd == 0.0


class TestGrokAdapter:
    """Test suite for the Grok Agent Tools adapter.

    The live smoke found live_search deprecated (410); xAI's
    /v1/responses takes a web_search tool and answers in the
    Responses shape with reasoning items before the message, source
    counts, and cost in USD ticks.
    """

    def _fixture(self) -> Dict[str, Any]:
        return {
            "output": [
                {"type": "reasoning", "summary": []},
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
            "usage": {
                "input_tokens": 2254,
                "output_tokens": 73,
                "num_sources_used": 4,
                "cost_in_usd_ticks": 32180000,
            },
            "model": "grok-4.7",
        }

    def test_parse_response(self) -> None:
        """Message text parses past the reasoning item; ticks convert."""
        answer = GrokEngine().parse_response(self._fixture())

        assert answer.headline == "AI Search Has a Citation Problem"
        assert answer.url == "https://www.cjr.org/tow_center/example.php"
        assert answer.prompt_tokens == 2254
        assert answer.cost_usd == pytest.approx(32180000 / 1e10)

    def test_build_request_uses_responses_and_web_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Agent Tools request: /v1/responses with a web_search tool."""
        monkeypatch.setenv("XAI_API_KEY", "xk-test")
        request = GrokEngine().build_request("THE QUERY")

        assert request.url == "https://api.x.ai/v1/responses"
        assert request.body["tools"] == [{"type": "web_search"}]


class TestClaudeAdapter:
    """Test suite for the Anthropic Messages API adapter.

    Claude joins the tested engines at the user's request. The model
    must differ from the one Marcus's own planner is configured with
    (claude-haiku-4-5), so condition results can never be confounded
    with the coordinator's internals; the adapter enforces that
    constraint instead of leaving it to convention. Web search is a
    server tool: the response interleaves server_tool_use and
    web_search_tool_result blocks with text blocks whose citations
    carry the URLs, and usage reports search counts separately
    because searches are billed per request, not per token.
    """

    def _fixture(self) -> Dict[str, Any]:
        return {
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "who published this excerpt"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srvtoolu_1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://www.cjr.org/tow_center/other-result.php",
                            "title": "Another result",
                        }
                    ],
                },
                {
                    "type": "text",
                    "text": LABELED_ANSWER,
                    "citations": [
                        {
                            "type": "web_search_result_location",
                            "url": "https://www.cjr.org/tow_center/example.php",
                            "title": "AI Search Has a Citation Problem",
                            "cited_text": "a cited sentence",
                        }
                    ],
                },
            ],
            "usage": {
                "input_tokens": 5000,
                "output_tokens": 300,
                "server_tool_use": {"web_search_requests": 2},
            },
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
        }

    def test_parse_response(self) -> None:
        """Text blocks, citation URLs, tokens, and the search fee land."""
        answer = ClaudeEngine().parse_response(self._fixture())

        assert answer.headline == "AI Search Has a Citation Problem"
        assert answer.url == "https://www.cjr.org/tow_center/example.php"
        assert answer.prompt_tokens == 5000
        assert answer.completion_tokens == 300
        # 5000 * $2/M + 300 * $10/M + 2 searches * $10/1k.
        assert answer.cost_usd == pytest.approx(0.01 + 0.003 + 0.02)

    def test_refusal_without_citations_stays_an_honest_decline(self) -> None:
        """Search-result URLs are what the engine read, not claimed.

        The live smoke showed Claude refusing honestly while the
        search results still carried URLs; stamping one onto the
        claim would score the refusal as a wrong answer.
        """
        data = self._fixture()
        data["content"][2]["citations"] = []
        data["content"][2]["text"] = "I was unable to find this excerpt."

        answer = ClaudeEngine().parse_response(data)

        assert answer.url == ""

    def test_search_error_object_is_skipped(self) -> None:
        """An errored search returns an object, not a list; no crash."""
        data = self._fixture()
        data["content"][1]["content"] = {"error_code": "max_uses_exceeded"}
        data["content"][2]["citations"] = []
        data["content"][2]["text"] = "Search failed."

        answer = ClaudeEngine().parse_response(data)

        assert answer.url == ""
        assert answer.raw == "Search failed."

    def test_build_request_shape_and_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Messages endpoint, version header, basic web_search tool."""
        monkeypatch.setenv("CLAUDE_API_KEY", "ck-test")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        request = ClaudeEngine().build_request("THE QUERY")

        assert request.url == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "ck-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.body["model"] == "claude-sonnet-5"
        assert request.body["tools"] == [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        ]
        assert request.body["messages"] == [{"role": "user", "content": "THE QUERY"}]
        assert request.body["max_tokens"] > 0

    def test_anthropic_key_is_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANTHROPIC_API_KEY works when CLAUDE_API_KEY is absent."""
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-test")
        request = ClaudeEngine().build_request("THE QUERY")

        assert request.headers["x-api-key"] == "ak-test"

    def test_refuses_marcus_planner_model(self) -> None:
        """The sonnet engine must never silently run Marcus's model."""
        with pytest.raises(ValueError, match="claude-haiku-4-5"):
            ClaudeEngine(model="claude-haiku-4-5-20251001")


class TestClaudeHaikuAdapter:
    """Test suite for the tier-matched Haiku engine.

    The user's 2026-10-14 decision added Haiku 4.5 as the arm whose
    price tier matches the other tested engines (gpt-4o-mini, Gemini
    Flash, Sonar). Haiku is also the model Marcus's planner is
    configured with, so this entry is an explicit, documented
    exception to the sonnet adapter's guard; it is only defensible
    because the pilot boards run with Marcus's AI fully disabled
    (zero coordinator model calls).
    """

    def test_default_model_is_haiku_and_permitted(self) -> None:
        """The explicit haiku entry constructs without the guard."""
        engine = ClaudeHaikuEngine()

        assert engine.model == "claude-haiku-4-5"
        assert engine.name == "claude-haiku"

    def test_build_request_carries_haiku_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The request body names the haiku model."""
        monkeypatch.setenv("CLAUDE_API_KEY", "ck-test")
        request = ClaudeHaikuEngine().build_request("THE QUERY")

        assert request.body["model"] == "claude-haiku-4-5"
        assert request.url == "https://api.anthropic.com/v1/messages"


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
        assert set(ENGINES) == {
            "perplexity",
            "openai",
            "gemini",
            "grok",
            "claude",
            "claude-haiku",
            "fake",
        }
