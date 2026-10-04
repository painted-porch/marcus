"""Shared robots-respecting fetch cache for the Tow replication (#737).

One fetcher serves the verifier in Condition B (on the record) and
the filter in Condition C (verifier with no board), with a
persistent cache keyed by URL, so a page that moves or dies between
runs scores identically in both conditions. That shared cache is a
preregistered requirement of the protocol (step 9).

Three rules:

- robots.txt is honored with an honest user agent, and a block is a
  recorded outcome ("unverifiable", which counts as declined), never
  something to work around.
- Failures are results, not exceptions: a 404, a timeout, or a dead
  domain all come back as a ``FetchResult`` the verdict logic reads.
- Every outcome is cached to disk, including blocks and errors, so a
  re-run replays the exact same observations.
"""

import hashlib
import json
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

USER_AGENT = (
    "MarcusTowReplication/1.0 "
    "(+https://github.com/painted-porch/marcus; research replication; "
    "respects robots.txt)"
)

MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT = 20.0


@dataclass
class FetchResult:
    """The complete, cacheable outcome of fetching one URL.

    Parameters
    ----------
    url : str
        The URL as requested.
    final_url : str
        Where the request ended after redirects.
    status : Optional[int]
        HTTP status, or None when no response existed (network error
        or robots block).
    html : str
        Raw page HTML ("" when unavailable).
    text : str
        Extracted page text ("" when unavailable).
    robots_blocked : bool
        True when robots.txt disallowed the fetch.
    error : str
        Network-level error description ("" when none).
    fetched_at : str
        UTC ISO timestamp of the original fetch.
    from_cache : bool
        True when this result was replayed from the cache.
    """

    url: str
    final_url: str
    status: Optional[int]
    html: str
    text: str
    robots_blocked: bool
    error: str
    fetched_at: str
    from_cache: bool = False


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script, style, and noscript."""

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())


def html_to_text(html: str) -> str:
    """Extract visible text from HTML.

    Parameters
    ----------
    html : str
        Raw page HTML.

    Returns
    -------
    str
        Whitespace-joined visible text.
    """
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML still yields chunks
        pass
    return " ".join(extractor._chunks)


class FetchCache:
    """Robots-respecting fetcher with a persistent per-URL cache.

    Parameters
    ----------
    cache_dir : str
        Directory for cached results (created if absent).
    opener : Optional[Callable[..., Any]]
        urllib-compatible opener; tests inject a fake, production
        uses ``urllib.request.urlopen``.
    timeout : float
        Per-request timeout in seconds.
    user_agent : str
        Sent on every request and used for robots evaluation.
    """

    def __init__(
        self,
        cache_dir: str,
        opener: Optional[Callable[..., Any]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.user_agent = user_agent
        self._robots: Dict[str, urllib.robotparser.RobotFileParser] = {}

    def fetch(self, url: str) -> FetchResult:
        """Fetch one URL, replaying from the cache when possible.

        Parameters
        ----------
        url : str
            The URL to fetch.

        Returns
        -------
        FetchResult
            The cached or fresh outcome.
        """
        cache_path = self._cache_path(url)
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            data["from_cache"] = True
            return FetchResult(**data)

        result = self._fetch_fresh(url)
        payload = asdict(result)
        payload["from_cache"] = False
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return result

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _fetch_fresh(self, url: str) -> FetchResult:
        now = datetime.now(timezone.utc).isoformat()
        if not self._robots_allowed(url):
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                html="",
                text="",
                robots_blocked=True,
                error="",
                fetched_at=now,
            )

        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(MAX_BYTES)
                html = body.decode("utf-8", errors="replace")
                return FetchResult(
                    url=url,
                    final_url=response.geturl(),
                    status=getattr(response, "status", 200),
                    html=html,
                    text=html_to_text(html),
                    robots_blocked=False,
                    error="",
                    fetched_at=now,
                )
        except urllib.error.HTTPError as http_err:
            return FetchResult(
                url=url,
                final_url=url,
                status=http_err.code,
                html="",
                text="",
                robots_blocked=False,
                error=f"HTTP {http_err.code}",
                fetched_at=now,
            )
        except Exception as net_err:  # noqa: BLE001 - failures are results
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                html="",
                text="",
                robots_blocked=False,
                error=str(net_err)[:200],
                fetched_at=now,
            )

    def _robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            robots_body = self._read_robots(f"{origin}/robots.txt")
            if robots_body is None:
                # No robots.txt (or unreachable): fetching is permitted,
                # the standard interpretation.
                parser.parse([])
            else:
                parser.parse(robots_body.splitlines())
            self._robots[origin] = parser
        return parser.can_fetch(self.user_agent, url)

    def _read_robots(self, robots_url: str) -> Optional[str]:
        request = urllib.request.Request(
            robots_url, headers={"User-Agent": self.user_agent}
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return str(response.read(MAX_BYTES).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - unreachable robots means allowed
            return None
