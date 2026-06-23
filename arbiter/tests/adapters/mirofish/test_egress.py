"""Egress firewall tests for MiroFish (A2) — allowlist, blocklist, and the
loopback-only inference-egress contract."""
from __future__ import annotations

import pytest

from arbiter.adapters.mirofish.egress import (
    LOOPBACK_HOSTS,
    EgressViolation,
    check_egress,
    check_inference_egress,
)


# ---------------------------------------------------------------------------
# check_egress — allowlist (data sources are permitted)
# ---------------------------------------------------------------------------


def test_egress_allows_filing_host() -> None:
    """A known SEC EDGAR URL must pass check_egress without raising."""
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    assert check_egress(url) == url


def test_egress_allows_localhost() -> None:
    """The self-hosted MiroFish endpoint on localhost must be permitted."""
    url = "http://localhost:8765/analyze"
    assert check_egress(url) == url


def test_egress_allows_alpaca_data() -> None:
    """Alpaca data endpoint (structured OHLCV) must be permitted."""
    url = "https://data.alpaca.markets/v2/stocks/AAPL/bars"
    assert check_egress(url) == url


# ---------------------------------------------------------------------------
# check_egress — blocklist (news/social/unknown hosts rejected)
# ---------------------------------------------------------------------------


def test_egress_blocks_news_host() -> None:
    with pytest.raises(EgressViolation, match="blocked keyword"):
        check_egress("https://newsapi.org/v2/everything?q=AAPL")


def test_egress_blocks_bloomberg() -> None:
    with pytest.raises(EgressViolation):
        check_egress("https://bloomberg.com/markets/stocks/AAPL")


def test_egress_blocks_twitter() -> None:
    with pytest.raises(EgressViolation):
        check_egress("https://twitter.com/search?q=AAPL")


def test_egress_blocks_unknown_host() -> None:
    with pytest.raises(EgressViolation, match="not on the allowlist"):
        check_egress("https://some-random-data-vendor.io/api/AAPL")


def test_egress_blocks_reuters() -> None:
    with pytest.raises(EgressViolation):
        check_egress("https://reuters.com/markets/companies/AAPL.OQ")


def test_egress_substring_keyword_match_is_intentional() -> None:
    """Substring keyword matching is over-broad BY DESIGN (fail-closed toward
    independence).  Pin it so the behavior is intentional, not accidental:
    a host merely *containing* a blocked keyword is rejected."""
    # "fool" is a blocked keyword → "foolproof.io" must be rejected.
    with pytest.raises(EgressViolation, match="blocked keyword"):
        check_egress("https://foolproof.io/api")


# ---------------------------------------------------------------------------
# check_inference_egress — loopback-only for the /analyze POST
# ---------------------------------------------------------------------------


def test_inference_egress_allows_localhost() -> None:
    url = "http://localhost:8765/analyze"
    assert check_inference_egress(url) == url


def test_inference_egress_allows_loopback_ip() -> None:
    url = "http://127.0.0.1:8765/analyze"
    assert check_inference_egress(url) == url


def test_inference_egress_rejects_allowlisted_but_nonlocal() -> None:
    """data.sec.gov is allowlisted for data fetches but is NOT loopback —
    it must be rejected as a MiroFish inference endpoint."""
    with pytest.raises(EgressViolation, match="not loopback"):
        check_inference_egress("https://data.sec.gov/analyze")


def test_inference_egress_rejects_cloud_metadata() -> None:
    """The cloud-metadata link-local address must be rejected (SSRF guard)."""
    with pytest.raises(EgressViolation):
        check_inference_egress("http://169.254.169.254/analyze")


def test_inference_egress_still_blocks_news_keyword() -> None:
    """The inference gate runs the keyword/allowlist check first, so a
    news-keyword host is rejected before the loopback check."""
    with pytest.raises(EgressViolation):
        check_inference_egress("http://news.local/analyze")


def test_loopback_hosts_is_strict_subset_of_nothing_external() -> None:
    """Sanity: LOOPBACK_HOSTS is exactly the three loopback names."""
    assert LOOPBACK_HOSTS == frozenset({"localhost", "127.0.0.1", "::1"})


# ---------------------------------------------------------------------------
# Scheme guard (SSRF) — only http/https may leave the process
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost/analyze",
        "gopher://localhost:8765/_analyze",
        "dict://localhost:8765/analyze",
        "ftp://127.0.0.1/analyze",
    ],
)
def test_egress_blocks_non_http_scheme(url: str) -> None:
    """A non-http(s) scheme that still parses a loopback host is an SSRF
    vector and must be rejected at the firewall — not merely at the
    http_client base-URL check."""
    with pytest.raises(EgressViolation, match="scheme"):
        check_egress(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost/analyze",
        "gopher://127.0.0.1:8765/_x",
    ],
)
def test_inference_egress_blocks_non_http_scheme(url: str) -> None:
    """The inference gate also rejects non-http schemes (scheme check runs
    inside the shared check_egress gate)."""
    with pytest.raises(EgressViolation, match="scheme"):
        check_inference_egress(url)


def test_egress_allows_http_and_https_schemes() -> None:
    """The two permitted schemes still pass for an allowlisted host."""
    assert check_egress("http://localhost:8765/analyze")
    assert check_egress("https://data.sec.gov/x")


# ---------------------------------------------------------------------------
# DNS-rebind-style / SSRF hostnames — fail closed (not on loopback allowlist)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # DNS-rebind decorations of a loopback literal/name.
        "http://127.0.0.1.attacker.com/analyze",
        "http://localhost.attacker.com/analyze",
        # Userinfo trick: real host is evil.com, not localhost.
        "http://localhost@evil.com/analyze",
        # Alternate encodings of 127.0.0.1 are NOT in the allowlist (strict).
        "http://2130706433/analyze",  # decimal form of 127.0.0.1
        "http://0.0.0.0:8765/analyze",  # wildcard bind addr, not loopback
    ],
)
def test_inference_egress_rejects_dns_rebind_and_alt_encodings(url: str) -> None:
    """A loopback look-alike host must be rejected: the allowlist is exact
    (localhost / 127.0.0.1 / ::1) so rebind decorations, userinfo tricks, and
    alternate IP encodings all fail closed."""
    with pytest.raises(EgressViolation):
        check_inference_egress(url)
