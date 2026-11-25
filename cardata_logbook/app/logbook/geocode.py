"""Käänteinen geokoodaus OpenStreetMap Nominatimilla (enintään 1 pyyntö/s, välimuisti).

Välimuisti on jaettu autojen kesken, koska se sisältää vain koordinaatti -> osoite -parit
eikä mitään ajopäiväkirjatietoa.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

_LOG = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"


def format_address(data: dict) -> str:
    addr = data.get("address") or {}
    road = addr.get("road") or addr.get("pedestrian") or addr.get("footway") or addr.get("neighbourhood")
    number = addr.get("house_number")
    place = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("suburb")
        or addr.get("county")
    )
    name = data.get("name") or addr.get("amenity") or addr.get("shop")
    street = " ".join(x for x in (road, number) if x)
    parts = [p for p in (street, place) if p]
    if name and name not in parts and not street:
        parts.insert(0, name)
    if parts:
        return ", ".join(parts)
    return data.get("display_name", "")


class Geocoder:
    def __init__(self, cache_path: str | Path, user_agent: str, zones=None):
        self.user_agent = user_agent
        self.zones = zones or []  # [(name, Zone)]
        self._lock = asyncio.Lock()
        self._last = 0.0
        p = Path(cache_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, address TEXT, fetched_at REAL)")
        self.db.commit()

    @staticmethod
    def _key(lat: float, lon: float) -> str:
        # 4 desimaalia ~ 11 m: lähekkäiset pisteet jakavat saman osoitteen
        return f"{lat:.4f},{lon:.4f}"

    def cached(self, lat: float, lon: float) -> str | None:
        row = self.db.execute("SELECT address FROM cache WHERE key=?", (self._key(lat, lon),)).fetchone()
        return row[0] if row else None

    async def reverse(self, lat: float | None, lon: float | None) -> str:
        if lat is None or lon is None:
            return ""
        hit = self.cached(lat, lon)
        if hit:
            return hit
        import aiohttp

        async with self._lock:
            hit = self.cached(lat, lon)
            if hit:
                return hit
            wait = 1.1 - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            params = {
                "format": "jsonv2",
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "zoom": "18",
                "addressdetails": "1",
                "accept-language": "fi",
            }
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": self.user_agent}) as session:
                    async with session.get(NOMINATIM_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status != 200:
                            _LOG.warning("Nominatim vastasi %s", r.status)
                            return ""
                        data = await r.json()
            except Exception as exc:  # verkkovirhe: osoite haetaan myöhemmin uudelleen
                _LOG.warning("Osoitehaku epäonnistui: %s", exc)
                return ""
            address = format_address(data)
            if address:
                self.db.execute(
                    "INSERT OR REPLACE INTO cache(key, address, fetched_at) VALUES (?,?,?)",
                    (self._key(lat, lon), address, time.time()),
                )
                self.db.commit()
            return address
