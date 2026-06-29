from datetime import datetime, timezone

from arbiter.safety.alerting import Alerting


def test_notify_posts_webhook(monkeypatch, tmp_path):
    posted = {}

    cfg = type("C", (), {"alert_webhook_url": "https://example/ntfy"})()
    a = Alerting(config=cfg, audit_path=str(tmp_path / "audit.jsonl"))

    def fake_post(*, tier, message, ctx, ts):
        posted.update(tier=tier, message=message)
    monkeypatch.setattr(a, "_post_webhook", fake_post)

    a.notify("Monday Refresh", "7 positions; CPI Thursday",
             as_of=datetime(2026, 6, 29, tzinfo=timezone.utc))
    assert posted["tier"] == "info"
    assert "Monday Refresh" in posted["message"]
