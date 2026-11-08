"""Strict time parsing, copied from Energy AI contracts.timestamp (reviewed source)."""
from datetime import datetime


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return dt.timestamp()
