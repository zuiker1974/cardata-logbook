"""Aikaleimojen käsittely.

Tietokantaan tallennetaan aina UTC-aika ISO 8601 -muodossa ("2026-11-17T06:00:00Z"),
jolloin merkkijonojen järjestys on sama kuin aikajärjestys myös kesäajan vaihtuessa.
Käyttöliittymä ja raportit näyttävät paikallisen ajan (oletus Europe/Helsinki).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def local_tz() -> ZoneInfo:
    name = os.environ.get("TZ") or "Europe/Helsinki"
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - virheellinen TZ
        return ZoneInfo("Europe/Helsinki")


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return to_iso(now_utc())


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def to_local(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    dt = parse_iso(value) if isinstance(value, str) else value
    return dt.astimezone(local_tz()) if dt else None


def local_naive_to_iso(value: str) -> str:
    """Lomakkeen paikallinen aika ("2026-11-17T08:30" tai "17.11.2026 08:30") UTC-merkkijonoksi."""
    value = value.strip()
    try:
        dt = datetime.strptime(value, "%d.%m.%Y %H:%M")
    except ValueError:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return to_iso(dt)


def fmt_date(value) -> str:
    dt = to_local(value)
    return dt.strftime("%d.%m.%Y") if dt else ""


def fmt_time(value) -> str:
    dt = to_local(value)
    return dt.strftime("%H:%M") if dt else ""


def fmt_datetime(value) -> str:
    dt = to_local(value)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else ""


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """Paikallisen kuukauden rajat UTC-merkkijonoina [alku, loppu)."""
    tz = local_tz()
    start = datetime(year, month, 1, tzinfo=tz)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    return to_iso(start), to_iso(end)


def year_bounds(year: int) -> tuple[str, str]:
    tz = local_tz()
    return to_iso(datetime(year, 1, 1, tzinfo=tz)), to_iso(datetime(year + 1, 1, 1, tzinfo=tz))


def local_year_month(value: str) -> tuple[int, int]:
    dt = to_local(value)
    return dt.year, dt.month


def minutes_between(a: str, b: str) -> int:
    return int(round((parse_iso(b) - parse_iso(a)).total_seconds() / 60))


def add_seconds(value: str, seconds: float) -> str:
    return to_iso(parse_iso(value) + timedelta(seconds=seconds))
