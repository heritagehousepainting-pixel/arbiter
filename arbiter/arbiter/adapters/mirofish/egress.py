"""Egress firewall for MiroFish (A2) — Lane 7.

INDEPENDENCE CONTRACT (INTERFACES.md §11.5 + design §3.8)
----------------------------------------------------------
MiroFish (A2) is a *structured-data reality-check* advisor.  Its job is
to evaluate an idea against filing data, balance sheets, and quantitative
factor models.  It must NEVER receive news feeds, social-media signals,
analyst commentary, or any other narrative input that would make it
correlated with A3 (the news/sentiment advisor).

This module is the **single enforcement point** for that independence
contract.  Every hostname that MiroFish is allowed to contact must appear
in ``ALLOWED_HOSTS``.  Any host outside this list raises
``EgressViolation`` before the request leaves the process.

Whitelist-drift risk: as MiroFish's data sources evolve, maintainers may
be tempted to add hosts that appear "structured" but actually carry
soft-information (e.g. earnings-call transcripts, analyst PDFs, or any
enriched data vendor that bundles news).  **Before adding a host, answer
two questions:**
  1. Does it serve exclusively machine-readable, point-in-time filing or
     quantitative factor data with no narrative component?
  2. Would A3 (NLP/sentiment lane) ever want to read the same source?
     If yes → reject; A2 and A3 must not share information sources.

Approved host categories:
  - SEC EDGAR (filing data, Form 4, 10-K, 8-K raw XBRL)
  - Exchange market-data APIs (OHLCV, order-book snapshots) — structured
  - Factor-model / fundamental-data vendors (e.g. Simfin, FinancialModelingPrep
    structured endpoints — NOT their news/press-release feeds)
  - Self-hosted MiroFish inference endpoint (localhost / private subnet)
"""
from __future__ import annotations

from urllib.parse import urlparse


class EgressViolation(ValueError):
    """Raised when a requested host is not on the MiroFish egress allowlist.

    Subclasses ``ValueError`` so callers can catch it without importing
    this module (duck-typing friendly), while still being distinct for
    specific handling.
    """


# ---------------------------------------------------------------------------
# ALLOWED_HOSTS — the only hosts MiroFish may contact.
#
# Rule: structured / filing data ONLY.  No news, no social, no transcripts.
# Each entry is a lower-case bare hostname (no scheme, no port, no path).
# Subdomains must be listed explicitly — wildcard matching is intentionally
# NOT supported to prevent accidental drift.
#
# To add a host: get two-eyes approval, document the data category above,
# confirm A3 does not use the same source.
# ---------------------------------------------------------------------------
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        # SEC EDGAR — Form 4, XBRL filings, structured disclosure data
        "www.sec.gov",
        "efts.sec.gov",
        "data.sec.gov",
        # Self-hosted MiroFish inference endpoint (local HTTP only)
        "localhost",
        "127.0.0.1",
        "::1",
        # Simfin structured fundamentals (balance sheet, income statement)
        # NOT their news or press-release endpoints
        "simfin.com",
        "api.simfin.com",
        # Financial Modeling Prep — structured fundamentals ONLY
        # (their /news and /press-releases endpoints are NOT permitted)
        "financialmodelingprep.com",
        "api.financialmodelingprep.com",
        # Alpaca market data (OHLCV bars — structured price data)
        "data.alpaca.markets",
    }
)

# ---------------------------------------------------------------------------
# LOOPBACK_HOSTS — the strict subset of ALLOWED_HOSTS that the MiroFish
# *inference* endpoint (the POST /analyze call) may use.  The inference
# service is self-hosted and MUST be reached over loopback only (the wire
# contract is localhost-only egress).  A non-loopback but allowlisted host
# (e.g. data.sec.gov) must NOT be accepted as MIROFISH_ENDPOINT.
# ---------------------------------------------------------------------------
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

# ---------------------------------------------------------------------------
# Blocked patterns — explicit deny list for hosts that look structured but
# carry narrative or social information.  Checked AFTER the allowlist so
# any future wildcard-style allowlist expansion is also blocked here.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Allowed URL schemes.  MiroFish only ever speaks HTTP(S) — any other scheme
# (ftp://, gopher://, file://, dict://, …) is a classic SSRF vector and must
# be rejected at the firewall, not merely at the http_client base-URL check.
# This is the single enforcement point, so the scheme guard belongs here too.
# ---------------------------------------------------------------------------
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


_BLOCKED_KEYWORDS: tuple[str, ...] = (
    "news",
    "social",
    "twitter",
    "x.com",
    "reddit",
    "seeking",
    "benzinga",
    "stocktwits",
    "bloomberg",
    "reuters",
    "wsj",
    "cnbc",
    "marketwatch",
    "thestreet",
    "fool",
    "zacks",
    "yahoo",
    "finviz",
    "sentiment",
    "transcript",
    "earningscall",
    "earnings-call",
)


def check_egress(url: str) -> str:
    """Validate that *url* is permitted under the MiroFish egress allowlist.

    Args:
        url: The full URL MiroFish intends to contact.

    Returns:
        The original *url* unchanged (pass-through for chaining).

    Raises:
        EgressViolation: If the host is not on the allowlist, or if the
            host contains a blocked keyword (independence guard).
        ValueError: If *url* cannot be parsed (malformed URL).

    Example::

        >>> check_egress("https://data.sec.gov/submissions/CIK0000320193.json")
        'https://data.sec.gov/submissions/CIK0000320193.json'

        >>> check_egress("https://news.ycombinator.com/item?id=1")
        # raises EgressViolation
    """
    parsed = urlparse(url)
    host = parsed.hostname  # lowercased by urlparse

    if not host:
        raise ValueError(f"Cannot parse hostname from URL: {url!r}")

    # Scheme guard (SSRF defense): only http/https may leave the process.
    # A non-http scheme that still parses a host (e.g. ftp://localhost/...,
    # gopher://localhost/...) is rejected here at the single enforcement point.
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise EgressViolation(
            f"MiroFish egress BLOCKED: scheme {scheme!r} is not permitted "
            f"(only http/https). Host {host!r} URL: {url!r}"
        )

    # Check blocked keywords first (fast path for obvious violations).
    host_lower = host.lower()
    for keyword in _BLOCKED_KEYWORDS:
        if keyword in host_lower:
            raise EgressViolation(
                f"MiroFish egress BLOCKED: host {host!r} contains blocked keyword "
                f"{keyword!r}. MiroFish (A2) must not receive news/social data — "
                f"that would violate independence from A3. "
                f"URL: {url!r}"
            )

    # Allowlist check.
    if host_lower not in ALLOWED_HOSTS:
        raise EgressViolation(
            f"MiroFish egress BLOCKED: host {host!r} is not on the allowlist. "
            f"MiroFish (A2) may only contact structured/filing data sources. "
            f"Allowed hosts: {sorted(ALLOWED_HOSTS)}. "
            f"URL: {url!r}"
        )

    return url


def check_inference_egress(url: str) -> str:
    """Validate *url* for the MiroFish **inference** POST (``/analyze``).

    This is a strict superset of :func:`check_egress`: it runs the full
    keyword/allowlist gate first, then additionally requires that the host be
    a loopback address (``localhost`` / ``127.0.0.1`` / ``::1``).  The
    self-hosted MiroFish service runs locally and the frozen wire contract is
    **localhost-only egress for the inference endpoint** — a non-loopback but
    otherwise allowlisted host (e.g. ``data.sec.gov``, or the cloud-metadata
    address ``169.254.169.254``) must be rejected when configured as
    ``MIROFISH_ENDPOINT``.

    Use :func:`check_egress` (not this) for any future structured data-source
    fetches; use this for the ``/analyze`` call only.

    Args:
        url: The full inference URL MiroFish intends to POST to.

    Returns:
        The original *url* unchanged on success.

    Raises:
        EgressViolation: If the host fails the allowlist/keyword gate, or is
            not a loopback address.
        ValueError: If *url* cannot be parsed (malformed URL).
    """
    check_egress(url)  # keyword + allowlist gate first (fail-closed)

    host = urlparse(url).hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise EgressViolation(
            f"MiroFish inference egress BLOCKED: host {host!r} is not loopback. "
            f"The MiroFish /analyze inference endpoint must be reached over "
            f"localhost only (allowed: {sorted(LOOPBACK_HOSTS)}). "
            f"URL: {url!r}"
        )

    return url
