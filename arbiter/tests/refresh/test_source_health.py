from datetime import datetime, timezone

from arbiter.refresh.source_health import scan_source_health, merge_flags
from arbiter.refresh.types import StaleFlag


def test_stale_when_ingest_age_exceeds_threshold():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    # form13f ingested 200 days ago -> stale; congress 1 day ago -> fresh
    ages = {"form13f": 200, "congress": 1}
    res = scan_source_health(conn=None, as_of=now,
                             ingest_age_fn=lambda c, s, a: ages.get(s))
    stale = {s.source for s in res.confirmed_stale()}
    assert "form13f" in stale and "congress" not in stale


def test_merge_flags_adds_matching_news_flag():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    res = scan_source_health(conn=None, as_of=now, ingest_age_fn=lambda c, s, a: 1)
    merged = merge_flags(res, [StaleFlag(source="activist_filers",
                                         reason="wound down", sources=[])])
    assert any(s.source == "activist_filers" and s.confirmed
               for s in merged.confirmed_stale())
