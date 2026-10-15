"""
Unit tests for the shared fetch cache (#737, steps 7 and 9).

One fetcher serves the verifier in Condition B and the filter in
Condition C, with a persistent cache keyed by URL, so a page that
moves between runs scores the same in both. It respects robots.txt
with an honest user agent, and a robots block is recorded as a
result, never worked around. No test touches the network: a fake
opener stands in for urllib.
"""

import io
import urllib.error
from pathlib import Path
from typing import Any, Dict, List

import pytest
from runners.tow_fetch import FetchCache, html_to_text

pytestmark = pytest.mark.unit

PAGE_HTML = (
    "<html><head><title>Example Page</title>"
    "<script>var x = 'ignore me';</script></head>"
    "<body><p>Some good news is that this storm should help.</p></body></html>"
)


class FakeResponse(io.BytesIO):
    """Minimal stand-in for urllib's HTTP response."""

    def __init__(self, body: str, url: str, status: int = 200) -> None:
        super().__init__(body.encode("utf-8"))
        self._url = url
        self.status = status
        self.headers: Dict[str, str] = {}

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class FakeOpener:
    """Record requested URLs and serve canned responses."""

    def __init__(self, pages: Dict[str, Any]) -> None:
        self.pages = pages
        self.calls: List[str] = []

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        url = request.get_full_url()
        self.calls.append(url)
        result = self.pages.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise urllib.error.HTTPError(url, 404, "not found", None, None)
        return result


def _cache(tmp_path: Path, opener: FakeOpener) -> FetchCache:
    """Build a FetchCache over the fake opener."""
    return FetchCache(cache_dir=str(tmp_path / "cache"), opener=opener)


class TestRobots:
    """Test suite for robots.txt handling."""

    def test_disallowed_url_is_not_fetched(self, tmp_path: Path) -> None:
        """A robots block records the refusal and never hits the page."""
        opener = FakeOpener(
            {
                "https://example.org/robots.txt": FakeResponse(
                    "User-agent: *\nDisallow: /article", "x"
                ),
            }
        )
        result = _cache(tmp_path, opener).fetch("https://example.org/article/1")

        assert result.robots_blocked is True
        assert result.status is None
        assert "https://example.org/article/1" not in opener.calls

    def test_allowed_url_is_fetched(self, tmp_path: Path) -> None:
        """An allowed URL fetches and extracts text."""
        opener = FakeOpener(
            {
                "https://example.org/robots.txt": FakeResponse(
                    "User-agent: *\nAllow: /", "x"
                ),
                "https://example.org/article/1": FakeResponse(
                    PAGE_HTML, "https://example.org/article/1"
                ),
            }
        )
        result = _cache(tmp_path, opener).fetch("https://example.org/article/1")

        assert result.robots_blocked is False
        assert result.status == 200
        assert "storm" in result.text

    def test_missing_robots_means_allowed(self, tmp_path: Path) -> None:
        """No robots.txt (404) means fetching is permitted."""
        opener = FakeOpener(
            {
                "https://example.org/article/1": FakeResponse(
                    PAGE_HTML, "https://example.org/article/1"
                ),
            }
        )
        result = _cache(tmp_path, opener).fetch("https://example.org/article/1")

        assert result.robots_blocked is False
        assert result.status == 200


class TestCache:
    """Test suite for the persistent cache."""

    def test_second_fetch_replays_from_cache(self, tmp_path: Path) -> None:
        """Conditions B and C must see identical fetch results."""
        opener = FakeOpener(
            {
                "https://example.org/article/1": FakeResponse(
                    PAGE_HTML, "https://example.org/article/1"
                ),
            }
        )
        cache = _cache(tmp_path, opener)

        first = cache.fetch("https://example.org/article/1")
        calls_after_first = len(opener.calls)
        second = cache.fetch("https://example.org/article/1")

        assert second.from_cache is True
        assert len(opener.calls) == calls_after_first
        assert second.text == first.text
        assert second.fetched_at == first.fetched_at

    def test_cache_survives_a_new_instance(self, tmp_path: Path) -> None:
        """The cache is a directory of files, not process memory."""
        opener = FakeOpener(
            {
                "https://example.org/article/1": FakeResponse(
                    PAGE_HTML, "https://example.org/article/1"
                ),
            }
        )
        _cache(tmp_path, opener).fetch("https://example.org/article/1")

        fresh_opener = FakeOpener({})
        result = _cache(tmp_path, fresh_opener).fetch("https://example.org/article/1")

        assert result.from_cache is True
        assert result.status == 200
        assert fresh_opener.calls == []

    def test_http_errors_are_results_not_exceptions(self, tmp_path: Path) -> None:
        """A 404 is a recorded outcome the verdict logic consumes."""
        opener = FakeOpener({})
        result = _cache(tmp_path, opener).fetch("https://example.org/gone")

        assert result.status == 404
        assert result.robots_blocked is False


class TestHtmlToText:
    """Test suite for page text extraction."""

    def test_strips_tags_and_scripts(self) -> None:
        """Scripts and markup vanish; the prose stays."""
        text = html_to_text(PAGE_HTML)

        assert "storm" in text
        assert "ignore me" not in text
        assert "<p>" not in text


def _public_resolver(host: str, port: int = 0, **kwargs: Any) -> List[Any]:
    """Resolve every hostname to a public address, no network."""
    return [(None, None, None, "", ("93.184.216.34", port))]


class TestAddressGuard:
    """Test suite for the untrusted-URL guard (Codex P1 on PR #739).

    Claimed URLs originate in model output. Without a guard, a claim
    of file:///etc/passwd, a loopback service, or the cloud metadata
    address 169.254.169.254 would make the verifier fetch it.
    """

    def test_non_http_scheme_is_blocked_without_network(self, tmp_path: Path) -> None:
        """file:// and friends never reach the opener."""
        opener = FakeOpener({})
        cache = FetchCache(
            cache_dir=str(tmp_path / "c"), opener=opener, resolver=_public_resolver
        )

        result = cache.fetch("file:///etc/passwd")

        assert result.status is None
        assert "scheme" in result.error
        assert opener.calls == []

    def test_private_loopback_and_linklocal_ips_are_blocked(
        self, tmp_path: Path
    ) -> None:
        """Literal internal addresses are refused before any connection."""
        opener = FakeOpener({})
        cache = FetchCache(
            cache_dir=str(tmp_path / "c"), opener=opener, resolver=_public_resolver
        )

        for url in (
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.8/x",
        ):
            result = cache.fetch(url)
            assert "private or local address" in result.error
        assert opener.calls == []

    def test_hostname_resolving_to_private_address_is_blocked(
        self, tmp_path: Path
    ) -> None:
        """A public-looking name that resolves internally is refused."""

        def private_resolver(host: str, port: int = 0, **kwargs: Any) -> List[Any]:
            return [(None, None, None, "", ("192.168.1.5", port))]

        opener = FakeOpener({})
        cache = FetchCache(
            cache_dir=str(tmp_path / "c"), opener=opener, resolver=private_resolver
        )

        result = cache.fetch("https://internal.example.org/x")

        assert "private or local address" in result.error
        assert opener.calls == []


class TestRedirectReCheck:
    """Test suite for the cross-origin redirect re-check (Codex P2)."""

    def test_redirect_to_robots_blocked_origin_discards_content(
        self, tmp_path: Path
    ) -> None:
        """A page that redirects to a disallowing origin records a block."""
        opener = FakeOpener(
            {
                "https://a.example.org/robots.txt": urllib.error.HTTPError(
                    "https://a.example.org/robots.txt", 404, "nf", None, None
                ),
                "https://a.example.org/page": FakeResponse(
                    PAGE_HTML, url="https://b.example.net/landed"
                ),
                "https://b.example.net/robots.txt": FakeResponse(
                    "User-agent: *\nDisallow: /", "https://b.example.net/robots.txt"
                ),
            }
        )
        cache = FetchCache(
            cache_dir=str(tmp_path / "c"), opener=opener, resolver=_public_resolver
        )

        result = cache.fetch("https://a.example.org/page")

        assert result.robots_blocked is True
        assert result.html == ""
        assert result.final_url == "https://b.example.net/landed"
