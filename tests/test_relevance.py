"""app/relevance.py: the "may be expired" staleness heuristic."""
from datetime import datetime, timedelta

from app.relevance import is_stale


def test_not_stale_without_promo_or_deadline():
    old_date = datetime.utcnow() - timedelta(days=1000)
    assert is_stale(old_date, None, has_promo_code=False, has_dated_offer=False) is False


def test_stale_when_promo_code_is_old():
    old_date = datetime.utcnow() - timedelta(days=200)
    assert is_stale(old_date, None, has_promo_code=True, has_dated_offer=False) is True


def test_not_stale_when_promo_code_is_recent():
    recent_date = datetime.utcnow() - timedelta(days=5)
    assert is_stale(recent_date, None, has_promo_code=True, has_dated_offer=False) is False


def test_falls_back_to_upload_date_when_no_saved_date():
    old_upload = datetime.utcnow() - timedelta(days=200)
    assert is_stale(None, old_upload, has_promo_code=False, has_dated_offer=True) is True


def test_unknown_age_with_dated_offer_flagged_to_be_safe():
    assert is_stale(None, None, has_promo_code=False, has_dated_offer=True) is True


def test_threshold_is_configurable(monkeypatch):
    import app.relevance as relevance_mod
    monkeypatch.setattr(relevance_mod, "STALE_THRESHOLD_MONTHS", 1)
    borderline = datetime.utcnow() - timedelta(days=40)
    assert is_stale(borderline, None, has_promo_code=True, has_dated_offer=False) is True
