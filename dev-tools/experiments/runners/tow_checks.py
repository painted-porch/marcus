"""The verifier's checks and verdict rules for the Tow replication (#737).

The verifier never calls a model. Given a claim (publisher, date,
URL) and the fetched page, it decides one of three verdicts with
rules fixed before the pilot runs:

- ``unverifiable``: robots.txt blocked the fetch, the request
  failed, or the page returned an error status. Counts as declined,
  never as correct.
- ``contradicted``: the page was fetched but the excerpt is not on
  it, the domain does not match the claimed publisher, or the page's
  declared date is more than one day from the claimed date.
- ``verified``: every determinable check passed.

The checks are deliberately transparent heuristics; the pilot's
preregistered thresholds (a Condition B confident-wrong rate above
10 percent means the verifier is being fooled) are what calibrate
them, and the grader, not the verifier, sees the ground truth.
"""

import difflib
import html as html_module
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from runners.tow_fetch import FetchResult

# Characters normalized to their plain equivalents before comparison.
_REPLACEMENTS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "…": "...",
    " ": " ",
}

_STOPWORDS = {"the", "a", "an", "of", "and"}


def normalize(text: str) -> str:
    """Normalize text for comparison.

    Unescapes HTML entities, maps typographic quotes and dashes to
    plain ASCII, lowercases, and collapses whitespace.

    Parameters
    ----------
    text : str
        Raw text.

    Returns
    -------
    str
        Comparable normalized form.
    """
    out = html_module.unescape(text)
    for fancy, plain in _REPLACEMENTS.items():
        out = out.replace(fancy, plain)
    out = out.lower()
    return " ".join(out.split())


# Excerpt matching (#752). The exact-substring rule declined correct
# pages whenever one word of a long excerpt differed from the page (a
# dash, a dropped word, an inline link); the decline audit measured a
# 10% false-decline rate from exactly this. The replacement aligns the
# excerpt's words against the page in order and requires both a high
# share of them and a contiguous run, so a page that merely quotes a
# phrase still fails (the run floor is eight words, or half the excerpt
# when the excerpt is shorter than sixteen). Thresholds were set against
# the audited cases:
# judged-wrong pages scored at most 0.32, judged-correct pages at least
# 0.95, so 0.85 sits in the gap with margin on both sides.
MIN_EXCERPT_COVERAGE = 0.85
MIN_EXCERPT_RUN = 8
_ANCHOR_WORDS = 4
_MAX_WINDOWS = 24
_WHOLE_PAGE_LIMIT = 4000

# Hosts whose URLs are opaque redirect wrappers around a cited page and
# whose robots policy disallows the wrapper path for every agent, so an
# honest fetcher cannot resolve them. Recorded as their own decline
# reason: the citation format, not the source, is what failed.
GROUNDING_REDIRECT_HOSTS = frozenset({"vertexaisearch.cloud.google.com"})


@dataclass(frozen=True)
class ExcerptMatch:
    """How well an excerpt matches a page, for the audit record.

    Parameters
    ----------
    found : bool
        True when coverage and the contiguous run both clear the floors.
    coverage : float
        Share of the excerpt's words matched on the page, in order.
    longest_run : int
        Longest contiguous run of matched words.
    excerpt_words : int
        Number of normalized words in the excerpt.
    """

    found: bool
    coverage: float
    longest_run: int
    excerpt_words: int


def _align(needle: List[str], hay: List[str]) -> Tuple[float, int]:
    """Align the excerpt words against a page slice, in order.

    Parameters
    ----------
    needle : List[str]
        Normalized excerpt words.
    hay : List[str]
        Normalized page words (the whole page or a window of it).

    Returns
    -------
    Tuple[float, int]
        ``(coverage, longest_run)``.
    """
    matcher = difflib.SequenceMatcher(a=needle, b=hay, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    matched = sum(b.size for b in blocks)
    longest = max((b.size for b in blocks), default=0)
    return matched / len(needle), longest


def _candidate_windows(needle: List[str], hay: List[str]) -> List[Tuple[int, int]]:
    """Find page regions worth aligning, anchored on shared word runs.

    Parameters
    ----------
    needle : List[str]
        Normalized excerpt words.
    hay : List[str]
        Normalized page words.

    Returns
    -------
    List[Tuple[int, int]]
        ``(start, end)`` slices of ``hay``, at most ``_MAX_WINDOWS``.
    """
    n = len(needle)
    size = _ANCHOR_WORDS
    index: Dict[Tuple[str, ...], List[int]] = {}
    for i in range(len(hay) - size + 1):
        index.setdefault(tuple(hay[i : i + size]), []).append(i)
    votes: Dict[int, int] = {}
    for j in range(n - size + 1):
        for pos in index.get(tuple(needle[j : j + size]), ()):
            start = max(0, pos - j - n)
            votes[start] = votes.get(start, 0) + 1
    starts = sorted(votes, key=lambda s: (-votes[s], s))[:_MAX_WINDOWS]
    return [(s, min(len(hay), s + 3 * n)) for s in sorted(starts)]


def match_excerpt(excerpt: str, page_text: str) -> ExcerptMatch:
    """Score how much of the excerpt appears on the page, in order.

    Parameters
    ----------
    excerpt : str
        The excerpt the case is about.
    page_text : str
        The fetched page's extracted text.

    Returns
    -------
    ExcerptMatch
        The match decision with the scores behind it.
    """
    needle = normalize(excerpt).split()
    hay = normalize(page_text).split()
    n = len(needle)
    if not needle or not hay:
        return ExcerptMatch(False, 0.0, 0, n)
    if f" {' '.join(needle)} " in f" {' '.join(hay)} ":
        return ExcerptMatch(True, 1.0, n, n)

    if len(hay) <= _WHOLE_PAGE_LIMIT or n < _ANCHOR_WORDS:
        windows = [(0, len(hay))]
    else:
        windows = _candidate_windows(needle, hay) or [(0, min(len(hay), 3 * n))]

    coverage, longest = 0.0, 0
    for start, end in windows:
        cov, run = _align(needle, hay[start:end])
        if (cov, run) > (coverage, longest):
            coverage, longest = cov, run
    run_floor = min(MIN_EXCERPT_RUN, max(1, n // 2))
    found = coverage >= MIN_EXCERPT_COVERAGE and longest >= run_floor
    return ExcerptMatch(found, round(coverage, 4), longest, n)


def excerpt_on_page(excerpt: str, page_text: str) -> bool:
    """Check whether the excerpt appears on the page.

    Parameters
    ----------
    excerpt : str
        The excerpt the case is about.
    page_text : str
        The fetched page's extracted text.

    Returns
    -------
    bool
        True when :func:`match_excerpt` finds it.
    """
    return match_excerpt(excerpt, page_text).found


def site_name_from_html(html: str) -> str:
    """Read the page's own declaration of its publisher.

    Prefers ``og:site_name``; falls back to the last segment of the
    page title after a pipe or hyphen separator, then the title.

    Parameters
    ----------
    html : str
        Raw page HTML.

    Returns
    -------
    str
        The declared site name, or "" when none is found.
    """
    og = re.search(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']',
        html,
        re.IGNORECASE,
    )
    if og:
        return og.group(1).strip()
    title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if title:
        text = html_module.unescape(title.group(1)).strip()
        for separator in ("|", " - ", " – ", " — "):
            if separator in text:
                return text.rsplit(separator, 1)[-1].strip()
        return text
    return ""


def published_date_from_html(html: str) -> Optional[date]:
    """Extract the page's declared publication date.

    Looks for ``article:published_time`` style metas, then
    ``datePublished`` in JSON-LD, then ``<time datetime=...>``.

    Parameters
    ----------
    html : str
        Raw page HTML.

    Returns
    -------
    Optional[date]
        The declared date, or None when the page declares none.
    """
    patterns = [
        r'property=["\'](?:article|og):published_time["\'][^>]+'
        r'content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]+'
        r'property=["\'](?:article|og):published_time["\']',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            parsed = _parse_iso_prefix(match.group(1))
            if parsed:
                return parsed
    return None


def _parse_iso_prefix(value: str) -> Optional[date]:
    """Parse the date from an ISO-ish timestamp prefix."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if not match:
        return None
    try:
        return date(*(int(g) for g in match.groups()))
    except ValueError:
        return None


def parse_claim_date(value: str) -> Optional[date]:
    """Parse a claimed or dataset date in its common formats.

    Handles ISO (2025-03-06), US numeric (03/14/2024), and long form
    (March 6, 2025).

    Parameters
    ----------
    value : str
        The date string.

    Returns
    -------
    Optional[date]
        The parsed date, or None when unparseable.
    """
    value = (value or "").strip()
    if not value:
        return None
    iso = _parse_iso_prefix(value)
    if iso:
        return iso
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def date_within_tolerance(
    claimed: Optional[date], declared: Optional[date], days: int = 1
) -> Optional[bool]:
    """Compare the claimed date with the page's declared date.

    Parameters
    ----------
    claimed : Optional[date]
        The date from the claim.
    declared : Optional[date]
        The date the page declares.
    days : int
        Tolerance in days (the protocol says one).

    Returns
    -------
    Optional[bool]
        True or False when both dates exist; None when either is
        missing, which the verdict treats as undeterminable rather
        than failing.
    """
    if claimed is None or declared is None:
        return None
    return abs((claimed - declared).days) <= days


def _compact(text: str) -> str:
    """Lowercase and strip to letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def canonical_url_from_html(html: str, page_url: str) -> str:
    """Read the page's own declaration of where its original lives.

    The gate-2 fix from the 2026-10-05 pilot: syndicated copies carry
    the excerpt but are not the source, and most declare the original
    through ``rel=canonical`` (primary) or ``og:url`` (fallback).

    Parameters
    ----------
    html : str
        Raw page HTML.
    page_url : str
        The fetched page's final URL, for resolving relative hrefs.

    Returns
    -------
    str
        The absolute canonical URL, or "" when the page declares none.
    """
    from urllib.parse import urljoin

    link = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ) or re.search(
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
        html,
        re.IGNORECASE,
    )
    if link:
        return str(urljoin(page_url, link.group(1).strip()))

    og = re.search(
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
        html,
        re.IGNORECASE,
    )
    if og:
        return str(urljoin(page_url, og.group(1).strip()))
    return ""


def _registrable_label(url: str) -> str:
    """Extract the main domain label for page-versus-canonical compare."""
    host = urlsplit(url).netloc.lower()
    labels = [p for p in host.split(".") if p and p != "www"]
    # Drop the public suffix (com, org, co.uk's uk, etc.) by taking
    # the label before the suffix; a two-label heuristic suffices for
    # the news domains in this dataset.
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else ""


def domain_matches_publisher(publisher: str, final_url: str, html: str) -> bool:
    """Check the claimed publisher against the page's identity.

    Two signals, in order of trust: the page's own ``og:site_name``
    (or title tail), then a token-level comparison between the
    publisher name and the domain label (e.g. "times" inside
    "nytimes"). A transparent heuristic the pilot calibrates.

    Parameters
    ----------
    publisher : str
        The claimed publisher name.
    final_url : str
        The fetched page's final URL after redirects.
    html : str
        Raw page HTML.

    Returns
    -------
    bool
        True when either signal matches.
    """
    pub_compact = _compact(publisher)
    if not pub_compact:
        return False

    site = site_name_from_html(html)
    site_compact = _compact(site)
    if site_compact and (pub_compact in site_compact or site_compact in pub_compact):
        return True

    host = urlsplit(final_url).netloc.lower()
    labels = [p for p in host.split(".") if p not in ("www", "com", "org", "net")]
    domain_label = _compact("".join(labels))
    if domain_label and (pub_compact in domain_label or domain_label in pub_compact):
        return True

    tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", publisher.lower())
        if t not in _STOPWORDS and len(t) >= 4
    ]
    return any(token in domain_label for token in tokens)


def decide_verdict(
    fetch: FetchResult, excerpt: str, claim: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Apply the fixed verdict rules to one fetched claim.

    Parameters
    ----------
    fetch : FetchResult
        The shared-cache fetch of the claimed URL.
    excerpt : str
        The excerpt the case is about.
    claim : Dict[str, Any]
        The claim under check: publisher, date, url.

    Returns
    -------
    Tuple[str, Dict[str, Any]]
        ``(verdict, checks)`` where verdict is ``verified``,
        ``contradicted``, or ``unverifiable``, and checks records
        every signal for the audit bundle.
    """
    checks: Dict[str, Any] = {
        "robots_blocked": fetch.robots_blocked,
        "http_status": fetch.status,
        "final_url": fetch.final_url,
        "fetched_at": fetch.fetched_at,
        "fetch_error": fetch.error,
    }

    if (
        fetch.robots_blocked
        or fetch.status is None
        or fetch.status >= 400
        or not fetch.html
    ):
        checks["decline_reason"] = _unverifiable_reason(fetch)
        return "unverifiable", checks

    match = match_excerpt(excerpt, fetch.text)
    found = match.found
    checks["excerpt_found"] = found
    checks["excerpt_coverage"] = match.coverage
    checks["excerpt_longest_run"] = match.longest_run

    # Gate-2 fix (pilot 2026-10-05): a page whose declared canonical
    # lives on another domain is a syndicated copy; it verifies the
    # content but not the attribution, so the claim is contradicted.
    canonical = canonical_url_from_html(fetch.html, fetch.final_url)
    checks["canonical_url"] = canonical
    syndicated = bool(canonical) and (
        _registrable_label(canonical) != _registrable_label(fetch.final_url)
    )
    checks["syndicated_copy"] = syndicated

    domain_ok = domain_matches_publisher(
        str(claim.get("publisher") or ""), fetch.final_url, fetch.html
    )
    checks["domain_match"] = domain_ok

    claimed_date = parse_claim_date(str(claim.get("date") or ""))
    declared_date = published_date_from_html(fetch.html)
    date_ok = date_within_tolerance(claimed_date, declared_date)
    checks["date_match"] = date_ok
    checks["page_date"] = declared_date.isoformat() if declared_date else None

    if not found:
        checks["decline_reason"] = "excerpt_missing"
        return "contradicted", checks
    if syndicated:
        checks["decline_reason"] = "syndicated_copy"
        return "contradicted", checks
    if not domain_ok:
        checks["decline_reason"] = "publisher_mismatch"
        return "contradicted", checks
    if date_ok is False:
        checks["decline_reason"] = "date_mismatch"
        return "contradicted", checks
    checks["decline_reason"] = None
    return "verified", checks


def _unverifiable_reason(fetch: FetchResult) -> str:
    """Name why a page could not be read at all.

    Parameters
    ----------
    fetch : FetchResult
        The failed fetch.

    Returns
    -------
    str
        One of ``opaque_redirect``, ``robots_blocked``,
        ``publisher_blocked`` (401, 403, 429, 451), ``dead_link``
        (404, 410), ``http_error`` (any other 4xx/5xx), ``fetch_error``
        (no response), or ``empty_page``.
    """
    if fetch.robots_blocked:
        host = urlsplit(fetch.url).netloc.lower()
        if host in GROUNDING_REDIRECT_HOSTS:
            return "opaque_redirect"
        return "robots_blocked"
    if fetch.status is None:
        return "fetch_error"
    if fetch.status in (401, 403, 429, 451):
        return "publisher_blocked"
    if fetch.status in (404, 410):
        return "dead_link"
    if fetch.status >= 400:
        return "http_error"
    return "empty_page"
