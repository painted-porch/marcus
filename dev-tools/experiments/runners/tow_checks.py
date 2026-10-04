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

import html as html_module
import re
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple
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


def excerpt_on_page(excerpt: str, page_text: str) -> bool:
    """Check whether the excerpt appears on the page, normalized.

    Parameters
    ----------
    excerpt : str
        The excerpt the case is about.
    page_text : str
        The fetched page's extracted text.

    Returns
    -------
    bool
        True when the full normalized excerpt is a substring of the
        normalized page text.
    """
    needle = normalize(excerpt)
    return bool(needle) and needle in normalize(page_text)


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

    Handles ISO (2025-03-06), US numeric (12/11/2024), and long form
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
        return "unverifiable", checks

    found = excerpt_on_page(excerpt, fetch.text)
    checks["excerpt_found"] = found

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
        return "contradicted", checks
    if not domain_ok:
        return "contradicted", checks
    if date_ok is False:
        return "contradicted", checks
    return "verified", checks
