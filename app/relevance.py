"""Heuristic staleness check: flag time-sensitive content that's likely expired."""
from datetime import datetime

from app.config import STALE_THRESHOLD_MONTHS


def is_stale(saved_date: datetime | None, upload_date: datetime | None,
             has_promo_code: bool, has_dated_offer: bool) -> bool:
    if not (has_promo_code or has_dated_offer):
        return False

    reference_date = saved_date or upload_date
    if not reference_date:
        # Unknown age + time-sensitive content: flag it to be safe.
        return True

    age_days = (datetime.utcnow() - reference_date).days
    return age_days > (STALE_THRESHOLD_MONTHS * 30)
