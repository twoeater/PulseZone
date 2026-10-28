"""Calendar rules use the server's local time, not a rolling 24-hour window."""
from datetime import datetime


def local_now():
    return datetime.now().astimezone()


def expired(row, now):
    if not row.get("maintenance") or not row.get("alerted_at"):
        return False
    cutoff = now.replace(hour=13, minute=0, second=0, microsecond=0)
    return now >= cutoff and datetime.fromisoformat(row["alerted_at"]) < cutoff


def suppressed(row, now):
    if not row.get("maintenance") or not row.get("alerted_at"):
        return False
    last = datetime.fromisoformat(row["alerted_at"]).astimezone(now.tzinfo)
    return last.date() == now.date()
