"""Answer-engine adapters for the Tow replication finder (#737, step 7).

One adapter per API-reachable engine from the protocol (Perplexity,
OpenAI with web search, Gemini with search grounding, Grok with
search), plus a fake engine for free end-to-end shakeouts.
Each adapter is a pure pair: ``build_request`` produces the HTTP
request, ``parse_response`` turns the provider's JSON into an
``EngineAnswer``. The network send is one thin method that unit
tests never call; the first live calls happen in the step 10 pilot
after the cost estimate is approved.

Parsing policy: the finder sends the study's exact query, so engines
answer in prose. The extractor pulls labeled fields where present
and falls back to citations and quoted titles. Every claim field is
always present in the evidence, empty string when the engine did not
provide it, because the typed-evidence gate checks key presence and
an honest empty URL becomes an honest unverifiable verdict.

Cost figures are illustrative estimates per the issue's protocol
(the cost store's actual figures replace them in the report); they
exist so the usage log and the step 10 estimate have real shapes.
"""

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

# Illustrative $ per million tokens (input, output). Replaced by
# provider billing at step 10; used only for running estimates.
_PRICES = {
    "perplexity": (1.0, 1.0),
    "openai": (0.15, 0.60),
    "gemini": (0.10, 0.40),
    "grok": (2.0, 10.0),
    "fake": (0.0, 0.0),
}

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_LABEL_RES = {
    "headline": re.compile(
        r"(?im)^\s*[*\-#>\s]*\**\s*(?:headline|title)\s*\**\s*[:\-]\s*(.+)$"
    ),
    "publisher": re.compile(
        r"(?im)^\s*[*\-#>\s]*\**\s*(?:publisher|publication|source|outlet)"
        r"\s*\**\s*[:\-]\s*(.+)$"
    ),
    "date": re.compile(
        r"(?im)^\s*[*\-#>\s]*\**\s*(?:date|published|publication date)"
        r"\s*\**\s*[:\-]\s*(.+)$"
    ),
    "url": re.compile(r"(?im)^\s*[*\-#>\s]*\**\s*(?:url|link)\s*\**\s*[:\-]\s*(.+)$"),
}
_QUOTED_TITLE_RE = re.compile(r"[\"“]([^\"”]{10,200})[\"”]")

# Prose fallbacks, shaped by real engine answers from the live smoke:
# 'published on December 11, 2024, by The Boston Globe.' and an
# italicized outlet name in an APA-style citation line.
_PROSE_DATE_RE = re.compile(
    r"(?:published|dated)(?:\s+on)?\s+([A-Z][a-z]+ \d{1,2},? \d{4})"
)
_BARE_DATE_RE = re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b")
_PROSE_PUBLISHER_RE = re.compile(
    r"(?:published[^.\n]*?|written[^.\n]*?)\bby\s+(?:the\s+)?"
    r"([A-Z][A-Za-z&'’. ]{2,50}?)(?=[.,;(\n])"
)
_ITALIC_OUTLET_RE = re.compile(r"\*([A-Z][^*\n]{2,60})\*")


@dataclass
class EngineAnswer:
    """One engine's answer to the study query, parsed into a claim.

    Parameters
    ----------
    headline : str
        Claimed headline ("" when the engine gave none).
    publisher : str
        Claimed publisher.
    date : str
        Claimed publication date, as the engine wrote it.
    url : str
        Claimed article URL.
    raw : str
        The engine's full response text, kept verbatim for the audit
        bundle; stripped at every board handoff.
    model : str
        Model name the provider reported.
    prompt_tokens : int
        Input tokens the provider reported.
    completion_tokens : int
        Output tokens the provider reported.
    cost_usd : float
        Illustrative cost estimate for this call.
    """

    headline: str = ""
    publisher: str = ""
    date: str = ""
    url: str = ""
    raw: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def claim_evidence(self) -> Dict[str, Any]:
        """Build the evidence payload a finder reports for a find task."""
        return {
            "headline": self.headline,
            "publisher": self.publisher,
            "date": self.date,
            "url": self.url,
            "raw": self.raw,
        }


@dataclass
class EngineRequest:
    """One HTTP request an adapter built.

    Parameters
    ----------
    url : str
        Endpoint URL.
    headers : Dict[str, str]
        Headers including authorization.
    body : Dict[str, Any]
        JSON body.
    """

    url: str
    headers: Dict[str, str]
    body: Dict[str, Any] = field(default_factory=dict)


def _clean(value: str) -> str:
    """Strip markdown emphasis and surrounding punctuation from a field."""
    out = value.strip().strip("*_`").strip()
    out = out.strip('"').strip("“”").strip()
    return out.rstrip(".,;")


def extract_claim_fields(text: str, citations: List[str]) -> Dict[str, str]:
    """Pull the four claim fields from an engine's prose answer.

    Parameters
    ----------
    text : str
        The engine's response text.
    citations : List[str]
        Citation URLs the provider returned alongside the text.

    Returns
    -------
    Dict[str, str]
        headline, publisher, date, url; "" where undetermined.
    """
    claim = {name: "" for name in ("headline", "publisher", "date", "url")}
    for name, pattern in _LABEL_RES.items():
        match = pattern.search(text)
        if match:
            claim[name] = _clean(match.group(1))

    if not claim["url"]:
        in_text = _URL_RE.search(text)
        if in_text:
            claim["url"] = in_text.group(0).rstrip(".,;)")
        elif citations:
            claim["url"] = citations[0]

    if not claim["headline"]:
        quoted = _QUOTED_TITLE_RE.search(text)
        if quoted:
            claim["headline"] = _clean(quoted.group(1))

    if not claim["date"]:
        date_match = _PROSE_DATE_RE.search(text) or _BARE_DATE_RE.search(text)
        if date_match:
            claim["date"] = _clean(date_match.group(1)).replace("  ", " ")
            if "," not in claim["date"]:
                claim["date"] = re.sub(
                    r"^([A-Z][a-z]+ \d{1,2}) (\d{4})$", r"\1, \2", claim["date"]
                )

    if not claim["publisher"]:
        by_match = _PROSE_PUBLISHER_RE.search(text)
        if by_match:
            claim["publisher"] = _clean(by_match.group(1))
        else:
            italic = _ITALIC_OUTLET_RE.search(text)
            if italic:
                claim["publisher"] = _clean(italic.group(1))

    return claim


class BaseEngine:
    """Shared adapter behavior: send, cost, claim assembly."""

    name = "base"
    env_key = ""
    default_model = ""
    timeout = 90.0

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or self.default_model

    def _api_key(self) -> str:
        key = os.environ.get(self.env_key, "")
        if not key:
            raise RuntimeError(
                f"{self.env_key} is not set; the {self.name} adapter "
                "cannot make live calls (and should not outside the "
                "approved pilot)."
            )
        return key

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        price_in, price_out = _PRICES[self.name]
        return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000

    def build_request(self, query: str) -> EngineRequest:
        """Build the provider request for one study query."""
        raise NotImplementedError

    def parse_response(self, data: Dict[str, Any]) -> EngineAnswer:
        """Parse the provider's JSON into an EngineAnswer."""
        raise NotImplementedError

    def query(self, query_text: str) -> EngineAnswer:
        """Send one live query. Never called by unit tests.

        Parameters
        ----------
        query_text : str
            The study query for one excerpt.

        Returns
        -------
        EngineAnswer
            The parsed answer.
        """
        request = self.build_request(query_text)
        if not request.url.startswith("https://"):
            raise ValueError(f"engine endpoint must be https, got {request.url!r}")
        http_request = urllib.request.Request(
            request.url,
            data=json.dumps(request.body).encode("utf-8"),
            headers={**request.headers, "Content-Type": "application/json"},
            method="POST",
        )
        # The https-only guard above pins the scheme; the endpoints are
        # fixed provider constants, never user input.
        with urllib.request.urlopen(  # nosec B310
            http_request, timeout=self.timeout
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return self.parse_response(data)


def _parse_responses_payload(data: Dict[str, Any]) -> tuple[str, List[str]]:
    """Pull message text and url_citation URLs from a Responses body.

    OpenAI, Perplexity's Agent API, and xAI's Agent Tools API all
    answer in this shape: an ``output`` list whose ``message`` items
    carry ``output_text`` parts with ``annotations`` (reasoning items
    and tool-call items are skipped).

    Parameters
    ----------
    data : Dict[str, Any]
        The provider's JSON body.

    Returns
    -------
    tuple[str, List[str]]
        The joined message text and the citation URLs in order.
    """
    texts: List[str] = []
    citations: List[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("text"):
                texts.append(part["text"])
            for annotation in part.get("annotations") or []:
                if annotation.get("url"):
                    citations.append(annotation["url"])
    return "\n".join(texts), citations


class PerplexityEngine(BaseEngine):
    """Perplexity Agent API (/v1/responses) with built-in search.

    The legacy Sonar chat endpoint answers 403 with a migration
    notice (found in the live smoke); the Agent API reports EXACT
    cost in ``usage.cost.total_cost``, which replaces our estimate.
    """

    name = "perplexity"
    env_key = "PERPLEXITY_API_KEY"
    default_model = "perplexity/sonar"

    def build_request(self, query: str) -> EngineRequest:
        """Build the Agent API request."""
        return EngineRequest(
            url="https://api.perplexity.ai/v1/responses",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            body={
                "model": self.model,
                "input": query,
                # Without the explicit tool the Agent API answers from
                # the bare model with no live search (live smoke: "I
                # don't have live web-search access here").
                "tools": [{"type": "web_search"}],
            },
        )

    def parse_response(self, data: Dict[str, Any]) -> EngineAnswer:
        """Parse the Responses body; cost comes from the provider."""
        content, citations = _parse_responses_payload(data)
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        provider_cost = float((usage.get("cost") or {}).get("total_cost") or 0.0)
        claim = extract_claim_fields(content, citations)
        return EngineAnswer(
            **claim,
            raw=content,
            model=str(data.get("model") or self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=provider_cost or self._cost(prompt_tokens, completion_tokens),
        )


class OpenAIEngine(BaseEngine):
    """OpenAI Responses API with the web_search tool."""

    name = "openai"
    env_key = "OPENAI_API_KEY"
    default_model = "gpt-4o-mini"

    def build_request(self, query: str) -> EngineRequest:
        """Build the Responses API request."""
        return EngineRequest(
            url="https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            body={
                "model": self.model,
                "tools": [{"type": "web_search"}],
                "input": query,
            },
        )

    def parse_response(self, data: Dict[str, Any]) -> EngineAnswer:
        """Parse output messages and url_citation annotations."""
        content, citations = _parse_responses_payload(data)
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        claim = extract_claim_fields(content, citations)
        return EngineAnswer(
            **claim,
            raw=content,
            model=str(data.get("model") or self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=self._cost(prompt_tokens, completion_tokens),
        )


class GeminiEngine(BaseEngine):
    """Gemini generateContent with Google Search grounding."""

    name = "gemini"
    env_key = "GEMINI_API_KEY"
    # Pinned to the mainline stable flash model as of 2026-10 (the
    # original study used 2.0 Flash; the replication uses today's).
    default_model = "gemini-3.5-flash"

    def build_request(self, query: str) -> EngineRequest:
        """Build the generateContent request."""
        return EngineRequest(
            url=(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent"
            ),
            headers={"x-goog-api-key": self._api_key()},
            body={
                "contents": [{"parts": [{"text": query}]}],
                "tools": [{"google_search": {}}],
            },
        )

    def parse_response(self, data: Dict[str, Any]) -> EngineAnswer:
        """Parse candidate text and grounding chunk URIs."""
        candidate = (data.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts") or []
        content = "\n".join(p.get("text", "") for p in parts if p.get("text"))
        citations = [
            chunk.get("web", {}).get("uri", "")
            for chunk in candidate.get("groundingMetadata", {}).get(
                "groundingChunks", []
            )
            if chunk.get("web", {}).get("uri")
        ]
        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        completion_tokens = int(usage.get("candidatesTokenCount") or 0)
        claim = extract_claim_fields(content, citations)
        return EngineAnswer(
            **claim,
            raw=content,
            model=str(data.get("modelVersion") or self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=self._cost(prompt_tokens, completion_tokens),
        )


class GrokEngine(BaseEngine):
    """Grok chat completions with live search."""

    name = "grok"
    env_key = "XAI_API_KEY"
    # Pinned to the mainline stable model as of 2026-10 ("grok-3"
    # returns HTTP 410 Gone).
    default_model = "grok-4.7"

    # xAI reports cost in "USD ticks"; 1e10 per dollar is the
    # management-API convention, recorded here as an assumption. The
    # usage log keeps tokens and source counts so the report can
    # re-derive cost from real billing.
    TICKS_PER_USD = 1e10

    def build_request(self, query: str) -> EngineRequest:
        """Build the Agent Tools request with web search.

        The chat-completions live search answers 410 Gone (found in
        the live smoke); /v1/responses with a web_search tool is the
        current path.
        """
        return EngineRequest(
            url="https://api.x.ai/v1/responses",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            body={
                "model": self.model,
                "input": query,
                "tools": [{"type": "web_search"}],
            },
        )

    def parse_response(self, data: Dict[str, Any]) -> EngineAnswer:
        """Parse the Responses body; convert tick-denominated cost."""
        content, citations = _parse_responses_payload(data)
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        ticks = float(usage.get("cost_in_usd_ticks") or 0.0)
        claim = extract_claim_fields(content, citations)
        return EngineAnswer(
            **claim,
            raw=content,
            model=str(data.get("model") or self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=(ticks / self.TICKS_PER_USD)
            or self._cost(prompt_tokens, completion_tokens),
        )


_DEFAULT_FAKE_CLAIM = {
    "headline": "A Made Up Headline About A Storm",
    "publisher": "Example News",
    "date": "2025-01-01",
    "url": "https://www.example.com/this-article-does-not-exist",
}


class FakeEngine(BaseEngine):
    """Free engine for end-to-end shakeouts; no network, no cost.

    The default claim carries a URL that will not verify, exercising
    the honest-decline path; pass a claim to drive the verified path.

    Parameters
    ----------
    claim : Optional[Dict[str, str]]
        The claim to return for every query.
    model : Optional[str]
        Ignored; present for interface parity.
    """

    name = "fake"
    env_key = ""
    default_model = "fake-engine"

    def __init__(
        self,
        model: Optional[str] = None,
        claim: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(model)
        self.claim = dict(claim or _DEFAULT_FAKE_CLAIM)

    def build_request(self, query: str) -> EngineRequest:
        """Fake engines build no requests."""
        return EngineRequest(url="fake://", headers={})

    def parse_response(self, data: Dict[str, Any]) -> EngineAnswer:
        """Fake engines parse nothing."""
        return self.query("")

    def query(self, query_text: str) -> EngineAnswer:
        """Return the canned claim, costing nothing."""
        return EngineAnswer(
            headline=self.claim.get("headline", ""),
            publisher=self.claim.get("publisher", ""),
            date=self.claim.get("date", ""),
            url=self.claim.get("url", ""),
            raw=("FAKE ENGINE RESPONSE (shakeout only): " + json.dumps(self.claim)),
            model=self.default_model,
        )


ENGINES: Dict[str, Type[BaseEngine]] = {
    "perplexity": PerplexityEngine,
    "openai": OpenAIEngine,
    "gemini": GeminiEngine,
    "grok": GrokEngine,
    "fake": FakeEngine,
}
