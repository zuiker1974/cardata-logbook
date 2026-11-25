"""Mittarijatkuvuus, pakolliset kentät ja yhteenvedot (yhden auton sisällä)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import timeutil
from .database import TRIP_TYPE_LABELS, VehicleDB


@dataclass
class Issue:
    kind: str  # gap / overlap / negative / missing_odometer / missing_purpose
    message: str
    blocking: bool = True
    prev_trip_id: int | None = None
    next_trip_id: int | None = None
    trip_id: int | None = None
    km: float | None = None
    from_time: str | None = None
    to_time: str | None = None
    from_odometer: float | None = None
    to_odometer: float | None = None
    ack_reason: str | None = None
    extra: dict = field(default_factory=dict)


def _km(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def odometer_issues(db: VehicleDB, tolerance_km: float, start_iso: str | None = None, end_iso: str | None = None) -> list[Issue]:
    """Aukot ja päällekkäisyydet peräkkäisten aktiivisten matkojen välillä.

    Aukko kuuluu sille jaksolle, jolla jälkimmäinen matka alkaa.
    """
    trips = db.list_trips()
    acks = db.gap_acks()
    issues: list[Issue] = []
    for t in trips:
        if start_iso and not (start_iso <= t["start_time"] < (end_iso or "9999")):
            continue
        if t["start_odometer"] is None or t["end_odometer"] is None:
            issues.append(Issue("missing_odometer", f"Matkalta {t['id']} puuttuu mittarilukema.", trip_id=t["id"]))
        elif t["end_odometer"] < t["start_odometer"]:
            issues.append(
                Issue(
                    "negative",
                    f"Matkan loppulukema {_km(t['end_odometer'])} on pienempi kuin alkulukema {_km(t['start_odometer'])}.",
                    trip_id=t["id"],
                )
            )
    for prev, nxt in zip(trips, trips[1:]):
        if start_iso and not (start_iso <= nxt["start_time"] < (end_iso or "9999")):
            continue
        if prev["end_odometer"] is None or nxt["start_odometer"] is None:
            continue
        diff = round(nxt["start_odometer"] - prev["end_odometer"], 1)
        if diff > tolerance_km:
            ack = acks.get((prev["id"], nxt["id"]))
            msg = (
                f"Kirjaamatta {_km(diff)} km, {timeutil.fmt_date(prev['end_time'])[:6]} klo {timeutil.fmt_time(prev['end_time'])}"
                f" – {timeutil.fmt_date(nxt['start_time'])[:6]} klo {timeutil.fmt_time(nxt['start_time'])}"
            )
            issues.append(
                Issue(
                    "gap",
                    msg,
                    blocking=ack is None,
                    prev_trip_id=prev["id"],
                    next_trip_id=nxt["id"],
                    km=diff,
                    from_time=prev["end_time"],
                    to_time=nxt["start_time"],
                    from_odometer=prev["end_odometer"],
                    to_odometer=nxt["start_odometer"],
                    ack_reason=ack["reason"] if ack else None,
                )
            )
        elif diff < -tolerance_km:
            issues.append(
                Issue(
                    "overlap",
                    f"Päällekkäiset mittarilukemat: matka alkaa lukemalla {_km(nxt['start_odometer'])},"
                    f" edellinen päättyi lukemaan {_km(prev['end_odometer'])}.",
                    prev_trip_id=prev["id"],
                    next_trip_id=nxt["id"],
                    km=diff,
                )
            )
    return issues


def purpose_issues(trips: list[dict]) -> list[Issue]:
    return [
        Issue("missing_purpose", f"Työajolta {timeutil.fmt_date(t['start_time'])} puuttuu tarkoitus.", trip_id=t["id"])
        for t in trips
        if t["trip_type"] == "business" and not (t["purpose"] or "").strip()
    ]


def period_issues(db: VehicleDB, year: int, month: int, tolerance_km: float) -> list[Issue]:
    start, end = timeutil.month_bounds(year, month)
    trips = db.list_trips(start, end)
    return odometer_issues(db, tolerance_km, start, end) + purpose_issues(trips)


def summarize(trips: list[dict]) -> dict:
    km = {k: 0.0 for k in TRIP_TYPE_LABELS}
    count = {k: 0 for k in TRIP_TYPE_LABELS}
    for t in trips:
        km[t["trip_type"]] += t["distance_km"] or 0
        count[t["trip_type"]] += 1
    total = sum(km.values())
    private_all = km["private"] + km["commute"]

    def pct(v):
        return round(100 * v / total, 1) if total else 0.0

    odo = [t for t in trips if t["start_odometer"] is not None]
    odo_end = [t for t in trips if t["end_odometer"] is not None]
    return {
        "total_km": round(total, 1),
        "business_km": round(km["business"], 1),
        "private_km": round(km["private"], 1),
        "commute_km": round(km["commute"], 1),
        "private_all_km": round(private_all, 1),
        "business_pct": pct(km["business"]),
        "private_all_pct": pct(private_all),
        "commute_pct": pct(km["commute"]),
        "count": len(trips),
        "counts": count,
        "odometer_start": min((t["start_odometer"] for t in odo), default=None),
        "odometer_end": max((t["end_odometer"] for t in odo_end), default=None),
    }


def odometer_reconciliation(trips: list[dict]) -> dict:
    """Jakson km mittarilukemista vs. kirjattujen ajojen summa (tavoite 0)."""
    s = summarize(trips)
    by_odo = None
    if s["odometer_start"] is not None and s["odometer_end"] is not None:
        by_odo = round(s["odometer_end"] - s["odometer_start"], 1)
    return {
        "by_odometer_km": by_odo,
        "logged_km": s["total_km"],
        "difference_km": round(by_odo - s["total_km"], 1) if by_odo is not None else None,
    }


def content_hash(vehicle_id: str, year: int, month: int, trips: list[dict]) -> str:
    """Tiiviste raportin sisällöstä (ajot ja niiden kentät), tulostetaan PDF:n alatunnisteeseen."""
    keys = (
        "id",
        "start_time",
        "end_time",
        "start_address",
        "end_address",
        "start_odometer",
        "end_odometer",
        "distance_km",
        "trip_type",
        "purpose",
        "comment",
        "source",
        "corrected",
    )
    payload = {
        "vehicle_id": vehicle_id,
        "period": f"{year}-{month:02d}",
        "trips": [{k: t.get(k) for k in keys} for t in trips],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
