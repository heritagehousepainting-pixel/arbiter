from datetime import datetime, timezone

from arbiter.refresh.position_news import scan_position_news
from arbiter.refresh.types import Severity


class _FakeClient:
    def __init__(self, news, sentiment, raises=False):
        self._news, self._sentiment, self._raises = news, sentiment, raises
    def get_company_news(self, ticker, from_date, to_date):
        if self._raises:
            raise RuntimeError("boom")
        return self._news
    def get_news_sentiment(self, ticker):
        return self._sentiment


def test_negative_sentiment_high_severity():
    c = _FakeClient(news=[{"headline": "DOJ probe"}],
                    sentiment={"sentiment_score": -0.6})
    [f] = scan_position_news(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc), c)
    assert f.ticker == "UBER" and f.available is True
    assert f.severity == Severity.HIGH and f.headlines == ["DOJ probe"]


def test_client_error_is_unavailable_not_raised():
    c = _FakeClient(news=[], sentiment={}, raises=True)
    [f] = scan_position_news(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc), c)
    assert f.available is False and f.severity == Severity.LOW
