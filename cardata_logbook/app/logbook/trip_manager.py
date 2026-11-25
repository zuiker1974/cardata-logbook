"""Ajotunnistuksen tilakone, yksi instanssi per auto.

Tilakone on puhdas: se saa tapahtumat aikaleimoineen (HA:n last_updated) ja kutsun
tick(now), eikä se lue kelloa itse. Näin sama logiikka toimii live-datalla,
historiapaikkauksessa ja testeissä.

530e:n datan erityispiirteet (mitattu HA:sta 9.–17.11.2026):
* sijainti päivittyy ajon aikana noin 2 min välein, ja jokainen päivitys tulee HA:hon
  noin 5 kertaa samoilla koordinaateilla -> samat koordinaatit ohitetaan (deduplikointi);
* liiketila jakaa ajot oikein, mutta pysäköinnin jälkeen tulee usein noin 30 s mittainen
  haamujakso (off -> on 30 s -> off), jonka aikana auto ei liiku -> lyhyt on-jakso, jonka
  aikana mittarilukema ei kasva, ei ole liikettä (ghost_motion_max_s);
* mittarilukema (1 km tarkkuus) tulee noin 5 s liiketilan off-tilan jälkeen.

Lisähavainnot 18.11.2026 (tests/test_replay_2026_11_18.py):
* haamujakso voi olla myös off -> 30 s -> on 7 min -> off. Ajon aikana pitkäkin on-jakso,
  jonka aikana auto ei siirry eikä mittari kasva, on haamu, jos ajolla on jo pysähdys;
* liiketilan off voi tulla noin 5 min todellisen pysähdyksen jälkeen. Mittarilukeman
  timestamp-attribuutti kertoo todellisen pysähdyshetken, joten sitä käytetään loppuaikana.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import timeutil
from .vehicle_registry import Settings, VehicleConfig

_LOG = logging.getLogger(__name__)

IDLE = "IDLE"
DRIVING = "DRIVING"
PAUSED_CHARGING = "PAUSED_CHARGING"
NOT_CONFIGURED = "NOT_CONFIGURED"

STATUS_LABELS = {
    IDLE: "Pysäköity",
    DRIVING: "Ajossa",
    PAUSED_CHARGING: "Lataustauolla",
    NOT_CONFIGURED: "Ei konfiguroitu",
}

INVALID_STATES = {"unavailable", "unknown", "none", ""}
MOVE_THRESHOLD_M = 50  # ajon aikana tätä pienempi siirtymä on GPS-kohinaa
LATE_DATA_WINDOW_S = 120  # pysähdyksen jälkeen saapuva viimeinen sijainti/mittari kuuluu vielä ajoon
STUCK_MOTION_FACTOR = 3  # liiketila jumissa "on": päätetään, jos liikettä ei ole 3 x aikakatkaisu
MAX_END_HINT_S = 900  # mittarin aikaleima saa aikaistaa loppuaikaa enintään 15 min
STOPPED_LABEL = "Pysähtynyt"  # ajo ei vielä päättynyt, mutta auto seisoo (odotetaan aikakatkaisua)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Zone:
    entity_id: str
    lat: float
    lon: float
    radius_m: float

    def contains(self, lat: float | None, lon: float | None) -> bool:
        if lat is None or lon is None:
            return False
        return haversine_m(self.lat, self.lon, lat, lon) <= self.radius_m + 50


@dataclass
class Event:
    role: str
    state: str
    attributes: dict
    ts: datetime


@dataclass
class Point:
    lat: float
    lon: float
    ts: str


@dataclass
class Segment:
    """Liiketilan on-jakso."""

    on: str
    off: str | None = None
    odo_at_on: float | None = None
    odo_increased: bool = False
    loc_at_on: list | None = None
    loc_far: bool = False
    moved: bool = False  # sijainti siirtyi jakson aikana (> MOVE_THRESHOLD_M)
    ghost: bool = False  # jakso tulkittiin haamuliikkeeksi


@dataclass
class ActiveTrip:
    start_time: str
    start_lat: float | None
    start_lon: float | None
    start_odometer: float | None
    start_soc: float | None
    last_lat: float | None
    last_lon: float | None
    end_odometer: float | None
    end_soc: float | None
    last_movement_time: str
    stop_time: str | None = None
    path_m: float = 0.0
    charging_stops: list = field(default_factory=list)
    current_stop: dict | None = None
    last_moved_time: str | None = None  # viimeinen sijainti, jossa auto oli siirtynyt
    end_hint: str | None = None  # mittarilukeman oma aikaleima pysähdyksen jälkeen


@dataclass
class MachineState:
    state: str = IDLE
    last_ts: str | None = None
    loc: Point | None = None
    odometer: float | None = None
    soc: float | None = None
    motion: bool | None = None
    charging: bool = False
    charging_since: str | None = None
    charging_method: str = ""
    hv_state: str = ""
    segment: Segment | None = None
    parked_lat: float | None = None
    parked_lon: float | None = None
    parked_odometer: float | None = None
    parked_soc: float | None = None
    parked_since: str | None = None
    first_move_ts: str | None = None  # ensimmäinen pieni siirtymä pysäköintipaikalta
    trip: ActiveTrip | None = None


class TripManager:
    def __init__(
        self,
        vehicle: VehicleConfig,
        settings: Settings,
        on_trip_finished: Callable[[dict], Any] | None = None,
        state_path: str | Path | None = None,
        zones: list[Zone] | None = None,
    ):
        self.vehicle = vehicle
        self.settings = settings
        self.on_trip_finished = on_trip_finished
        self.state_path = Path(state_path) if state_path else None
        self.zones: list[Zone] = zones or []
        self.stream_ok = True
        self.s = MachineState()
        self.has_motion = bool(vehicle.entity("motion"))
        self.ignored_ghosts = 0
        self.ignored_duplicates = 0
        self._load()

    # ------------------------------------------------------------- pysyvyys
    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.s = _state_from_dict(raw)
            _LOG.info("%s: palautettu tila %s", self.vehicle.id, self.s.state)
        except Exception:  # pragma: no cover - vioittunut tiedosto
            _LOG.exception("%s: aktiivisen ajon tilaa ei voitu lukea", self.vehicle.id)

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self.s), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---------------------------------------------------------------- tila
    @property
    def state(self) -> str:
        return self.s.state

    @property
    def status_label(self) -> str:
        t = self.s.trip
        if (
            self.s.state == DRIVING
            and t is not None
            and self.has_motion
            and self.s.motion is False
            and t.stop_time is not None
        ):
            return STOPPED_LABEL
        return STATUS_LABELS.get(self.s.state, self.s.state)

    @property
    def last_ts(self) -> str | None:
        return self.s.last_ts

    def snapshot(self) -> dict:
        return {
            "state": self.s.state,
            "status": self.status_label,
            "stream_ok": self.stream_ok,
            "odometer": self.s.odometer,
            "soc": self.s.soc,
            "charging": self.s.charging,
            "location": asdict(self.s.loc) if self.s.loc else None,
            "trip_start": self.s.trip.start_time if self.s.trip else None,
            "last_ts": self.s.last_ts,
            "ignored_ghosts": self.ignored_ghosts,
            "ignored_duplicates": self.ignored_duplicates,
        }

    def in_home_zone(self, lat: float | None, lon: float | None) -> bool:
        return any(z.contains(lat, lon) for z in self.zones)

    # ------------------------------------------------------------ tapahtumat
    def process(self, ev: Event) -> None:
        """Käsittelee yhden entiteettimuutoksen. Palauttaa nopeasti, jos arvo ei muuttunut."""
        state = (ev.state or "").strip()
        if ev.role != "location" and state.lower() in INVALID_STATES:
            return
        ts_iso = timeutil.to_iso(ev.ts)
        if self.s.last_ts and ts_iso < self.s.last_ts:
            ts_iso = self.s.last_ts  # myöhässä tullut tapahtuma: ei siirretä aikaa taaksepäin
        if self.s.trip is not None and self.s.state != IDLE:
            # Aikakatkaisut tarkistetaan ennen uutta tapahtumaa. Live-tilassa ajastin tekee
            # tämän 15 s välein, mutta historiapaikkauksessa muuten pitkän pysähdyksen jälkeinen
            # liike yhdistäisi kaksi ajoa.
            self.tick(timeutil.parse_iso(ts_iso))
        changed = False
        if ev.role == "location":
            changed = self._on_location(ev.attributes or {}, ts_iso)
        elif ev.role == "odometer":
            changed = self._on_odometer(state, ts_iso, ev.attributes or {})
        elif ev.role == "motion":
            changed = self._on_motion(state.lower(), ts_iso)
        elif ev.role == "soc":
            v = _float(state)
            if v is not None and v != self.s.soc:
                self.s.soc = v
                if self.s.trip:
                    self.s.trip.end_soc = v
                changed = True
        elif ev.role == "charging_state":
            changed = self._on_charging(state, ts_iso)
        elif ev.role == "charging_method":
            if state != self.s.charging_method:
                self.s.charging_method = state
                changed = True
        elif ev.role == "hv_system_state":
            self.s.hv_state = state
        if not changed:
            self.ignored_duplicates += 1
            return
        self.s.last_ts = ts_iso
        self.tick(timeutil.parse_iso(ts_iso))
        self.save()

    def _on_location(self, attrs: dict, ts: str) -> bool:
        lat, lon = _float(attrs.get("latitude")), _float(attrs.get("longitude"))
        if lat is None or lon is None:
            return False
        if self.s.loc and self.s.loc.lat == lat and self.s.loc.lon == lon:
            return False  # sama sijainti uudelleen (CarData kirjoittaa saman pisteen ~5 kertaa)
        prev = self.s.loc
        self.s.loc = Point(lat, lon, ts)
        if self.s.state == IDLE:
            if self.s.parked_lat is None:
                self._set_parked(lat, lon, self.s.odometer, ts)
                return True
            dist = haversine_m(self.s.parked_lat, self.s.parked_lon, lat, lon)
            if dist > MOVE_THRESHOLD_M and (
                self.s.first_move_ts is None
                or _secs(self.s.first_move_ts, ts) > self.settings.trip_end_timeout_s
            ):
                self.s.first_move_ts = ts
            if self.s.segment and self.s.segment.off is None and self.s.segment.loc_at_on:
                if haversine_m(*self.s.segment.loc_at_on, lat, lon) > self.settings.trip_start_distance_m:
                    self.s.segment.loc_far = True
            if dist > self.settings.trip_start_distance_m:
                self._start_trip(ts, reason=f"sijainti siirtyi {dist:.0f} m")
                self._trip_point(lat, lon, ts, moved=True, prev=None)
            return True
        moved = False
        t = self.s.trip
        if t is not None:
            ref_lat = t.last_lat if t.last_lat is not None else (prev.lat if prev else lat)
            ref_lon = t.last_lon if t.last_lon is not None else (prev.lon if prev else lon)
            moved = haversine_m(ref_lat, ref_lon, lat, lon) > MOVE_THRESHOLD_M
            seg = self.s.segment
            if seg and seg.off is None:
                if moved:
                    seg.moved = True
                if seg.loc_at_on and haversine_m(*seg.loc_at_on, lat, lon) > self.settings.trip_start_distance_m:
                    seg.loc_far = True
            self._trip_point(lat, lon, ts, moved=moved, prev=(ref_lat, ref_lon))
        return True

    def _on_odometer(self, state: str, ts: str, attrs: dict | None = None) -> bool:
        value = _float(state)
        if value is None or value == self.s.odometer:
            return False
        prev = self.s.odometer
        self.s.odometer = value
        seg = self.s.segment
        if seg and seg.off is None and seg.odo_at_on is not None and value > seg.odo_at_on:
            seg.odo_increased = True
        if self.s.state == IDLE:
            if self.s.parked_odometer is None:
                self.s.parked_odometer = value
                return True
            if value >= self.s.parked_odometer + 1:
                self._start_trip(ts, reason=f"mittarilukema kasvoi {value - self.s.parked_odometer:.0f} km")
                self.s.trip.end_odometer = value
                self._movement(ts)
            elif value < self.s.parked_odometer:
                _LOG.warning("%s: mittarilukema pieneni %s -> %s", self.vehicle.id, self.s.parked_odometer, value)
            return True
        t = self.s.trip
        if t is not None:
            increased = prev is not None and value > prev
            if increased and seg and seg.ghost and seg.off and _secs(seg.off, ts) <= LATE_DATA_WINDOW_S:
                # haamuksi tulkittu jakso olikin ajoa: mittari kasvoi heti sen jälkeen
                seg.ghost = False
                t.stop_time = seg.off
                t.end_hint = None
                self.ignored_ghosts = max(0, self.ignored_ghosts - 1)
            if increased:
                self._end_hint(ts, (attrs or {}).get("timestamp"))
            if t.end_odometer is None or value > t.end_odometer:
                t.end_odometer = value
                if prev is not None and value > prev:
                    self._movement(ts)
            if t.start_odometer is None:
                t.start_odometer = prev if prev is not None else value
        return True

    def _on_motion(self, state: str, ts: str) -> bool:
        on = state == "on"
        if state not in ("on", "off") or on == self.s.motion:
            return False
        self.s.motion = on
        if on:
            self.s.segment = Segment(
                on=ts,
                odo_at_on=self.s.odometer,
                loc_at_on=[self.s.loc.lat, self.s.loc.lon] if self.s.loc else None,
            )
            return True
        seg = self.s.segment
        if seg is None:
            return True
        seg.off = ts
        duration = (timeutil.parse_iso(ts) - timeutil.parse_iso(seg.on)).total_seconds()
        evidence = seg.odo_increased or seg.loc_far or seg.moved
        t = self.s.trip
        if t is not None and self.s.state in (DRIVING, PAUSED_CHARGING):
            # Pitkäkin on-jakso ilman siirtymää on haamu, jos ajolla on jo pysähdys
            # (530e: off -> 30 s -> on 7 min -> off pysäköinnin jälkeen).
            real = evidence or (duration >= self.settings.ghost_motion_max_s and t.stop_time is None)
            if real:
                t.stop_time = ts
                t.end_hint = None
                if self.s.state == PAUSED_CHARGING and (seg.odo_increased or seg.loc_far):
                    self._resume(seg.on)
            else:
                seg.ghost = True
                self.ignored_ghosts += 1
                _LOG.info("%s: haamuliike ohitettu (%.0f s, ei siirtymää)", self.vehicle.id, duration)
        elif not (seg.odo_increased or seg.loc_far):
            self.ignored_ghosts += 1
            _LOG.debug("%s: liike ilman siirtymää ohitettu (%.0f s)", self.vehicle.id, duration)
        return True

    def _on_charging(self, state: str, ts: str) -> bool:
        charging = state.upper().replace(" ", "_") in ("CHARGING", "CHARGING_ACTIVE")
        if charging == self.s.charging:
            return False
        self.s.charging = charging
        self.s.charging_since = ts if charging else None
        t = self.s.trip
        if t and t.current_stop and not charging:
            t.current_stop["charge_end"] = ts
            t.current_stop["soc_end"] = self.s.soc
        return True

    # ------------------------------------------------------------- apurit
    def _set_parked(self, lat, lon, odometer, ts) -> None:
        self.s.parked_lat, self.s.parked_lon = lat, lon
        if odometer is not None:
            self.s.parked_odometer = odometer
        self.s.parked_soc = self.s.soc
        self.s.parked_since = ts
        self.s.first_move_ts = None

    def _start_trip(self, ts: str, reason: str) -> None:
        seg = self.s.segment
        start = ts
        if seg is not None:
            off = seg.off
            recent = off is None or (
                timeutil.parse_iso(ts) - timeutil.parse_iso(off)
            ).total_seconds() <= self.settings.trip_end_timeout_s
            if recent and (self.s.parked_since is None or seg.on >= self.s.parked_since):
                start = min(seg.on, ts)
        fm = self.s.first_move_ts
        if (
            fm is not None
            and (self.s.parked_since is None or fm >= self.s.parked_since)
            and 0 <= _secs(fm, ts) <= self.settings.trip_end_timeout_s
        ):
            start = min(start, fm)  # auto lähti liikkeelle jo ennen kuin siirtymä ylitti rajan
        start_odo = self.s.parked_odometer if self.s.parked_odometer is not None else self.s.odometer
        self.s.trip = ActiveTrip(
            start_time=start,
            start_lat=self.s.parked_lat,
            start_lon=self.s.parked_lon,
            start_odometer=start_odo,
            start_soc=self.s.parked_soc if self.s.parked_soc is not None else self.s.soc,
            last_lat=self.s.parked_lat,
            last_lon=self.s.parked_lon,
            end_odometer=self.s.odometer,
            end_soc=self.s.soc,
            last_movement_time=ts,
            stop_time=seg.off if (seg and seg.off and seg.off >= start) else None,
        )
        self.s.first_move_ts = None
        self.s.state = DRIVING
        _LOG.info("%s: ajo alkoi %s (%s)", self.vehicle.id, start, reason)

    def _trip_point(self, lat, lon, ts, moved: bool, prev) -> None:
        t = self.s.trip
        if t is None:
            return
        if prev is not None and moved:
            t.path_m += haversine_m(prev[0], prev[1], lat, lon)
        elif prev is None and t.start_lat is not None:
            t.path_m += haversine_m(t.start_lat, t.start_lon, lat, lon)
        t.last_lat, t.last_lon = lat, lon
        if moved or prev is None:
            t.last_moved_time = ts
        if moved:
            self._movement(ts)

    def _movement(self, ts: str) -> None:
        t = self.s.trip
        if t is None:
            return
        late = (
            t.stop_time is not None
            and not self.s.motion
            and (timeutil.parse_iso(ts) - timeutil.parse_iso(t.stop_time)).total_seconds() <= LATE_DATA_WINDOW_S
        )
        t.last_movement_time = max(t.last_movement_time, ts)
        if late:
            return  # pysähdyksen jälkeen saapunut viimeinen lukema: ajo ei jatku
        if self.s.state == PAUSED_CHARGING:
            seg = self.s.segment
            short_open = (
                self.has_motion
                and self.s.motion
                and seg is not None
                and (timeutil.parse_iso(ts) - timeutil.parse_iso(seg.on)).total_seconds()
                < self.settings.ghost_motion_max_s
                and not seg.odo_increased
            )
            if not short_open:
                self._resume(ts)
            return
        if self.has_motion and self.s.motion:
            return  # liikkeessä, pysähdysaika asetetaan liiketilan off-hetkellä
        if self.has_motion and t.stop_time is not None:
            # liikettä ilman liiketilaa (liiketieto puuttui): ajo jatkuu
            t.stop_time = None
            t.end_hint = None

    def _resume(self, ts: str) -> None:
        t = self.s.trip
        if t is None:
            return
        stop = t.current_stop
        if stop:
            stop["end_time"] = stop.get("charge_end") or ts
            if stop.get("soc_end") is None:
                stop["soc_end"] = self.s.soc
            t.charging_stops.append(stop)
            t.current_stop = None
        t.stop_time = None
        t.end_hint = None
        self.s.state = DRIVING
        _LOG.info("%s: ajo jatkuu lataustauon jälkeen", self.vehicle.id)

    def _end_hint(self, ts: str, stamp: Any) -> None:
        """Mittarilukeman oma aikaleima kertoo todellisen pysähdyshetken, kun liiketilan
        off tuli myöhässä. Käytetään vain, jos se osuu viimeisen siirtymän ja off-hetken väliin."""
        t = self.s.trip
        if t is None or not stamp or t.stop_time is None or self.s.motion:
            return
        if _secs(t.stop_time, ts) > LATE_DATA_WINDOW_S or ts < t.stop_time:
            return
        try:
            hint = timeutil.to_iso(timeutil.parse_iso(str(stamp)))
        except Exception:
            return
        lower = max(t.start_time, t.last_moved_time or t.start_time)
        if lower <= hint <= t.stop_time and _secs(hint, t.stop_time) <= MAX_END_HINT_S:
            t.end_hint = hint

    def _stop_reference(self) -> str | None:
        """Hetki, jolloin auto pysähtyi, tai None jos auto on yhä liikkeessä."""
        t = self.s.trip
        if t is None:
            return None
        if self.has_motion and self.s.motion is not None:
            if self.s.motion:
                seg = self.s.segment
                return None if seg is not None else t.last_movement_time
            if t.stop_time is not None:
                late_limit = timeutil.add_seconds(t.stop_time, LATE_DATA_WINDOW_S)
                if t.last_movement_time <= late_limit:
                    return t.stop_time
            return t.last_movement_time
        return t.last_movement_time

    def _pause_allowed(self) -> bool:
        t = self.s.trip
        if t is None or not self.s.charging:
            return False
        if self.in_home_zone(t.last_lat, t.last_lon):
            return False
        if None not in (t.start_lat, t.start_lon, t.last_lat, t.last_lon) and haversine_m(
            t.start_lat, t.start_lon, t.last_lat, t.last_lon
        ) <= 2 * self.settings.trip_start_distance_m:
            return False  # lataus lähtöpaikalla (esim. kotona ilman kotivyöhykettä) päättää ajon
        is_dc = "DC" in (self.s.charging_method or "").upper()
        return is_dc or (self.vehicle.charging_pause_enabled and self.vehicle.charging_pause_timeout_s > 0)

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime) -> dict | None:
        """Tarkistaa aikakatkaisut. Palauttaa päättyneen matkan, jos sellainen syntyi."""
        t = self.s.trip
        if t is None or self.s.state == IDLE:
            return None
        now_iso = timeutil.to_iso(now)
        ref = self._stop_reference()
        if ref is None:
            # liiketila "on": suojaus jumiin jäänyttä liiketilaa vastaan
            seg = self.s.segment
            stuck_after = STUCK_MOTION_FACTOR * self.settings.trip_end_timeout_s
            if seg and (now - timeutil.parse_iso(max(seg.on, t.last_movement_time))).total_seconds() > stuck_after:
                ref = t.last_movement_time
            else:
                return None
        if not self.stream_ok:
            return None  # CarData-yhteys poikki: hiljaisuus ei tarkoita pysähdystä
        idle_s = (now - timeutil.parse_iso(ref)).total_seconds()
        if self.s.state == DRIVING and self._pause_allowed() and idle_s >= 60:
            self.s.state = PAUSED_CHARGING
            t.current_stop = {
                "start_time": self.s.charging_since or ref,
                "lat": t.last_lat,
                "lon": t.last_lon,
                "soc_start": self.s.soc,
                "method": "DC" if "DC" in (self.s.charging_method or "").upper() else "AC",
            }
            _LOG.info("%s: lataustauko alkoi", self.vehicle.id)
            self.save()
            return None
        if self.s.state == PAUSED_CHARGING:
            timeout = self.vehicle.charging_pause_timeout_s or self.settings.trip_end_timeout_s
            if "DC" in (self.s.charging_method or "").upper():
                timeout = max(timeout, self.settings.trip_end_timeout_s)
        else:
            timeout = self.settings.trip_end_timeout_s
        if idle_s < timeout:
            return None
        end = ref
        if t.end_hint and ref == t.stop_time and t.start_time <= t.end_hint <= ref:
            end = t.end_hint
        trip = self._finish(end)
        _LOG.info("%s: ajo päättyi %s (%s)", self.vehicle.id, end, now_iso)
        self.save()
        return trip

    def _finish(self, end_time: str) -> dict | None:
        t = self.s.trip
        assert t is not None
        self.s.trip = None
        self.s.state = IDLE
        end_lat = t.last_lat if t.last_lat is not None else (self.s.loc.lat if self.s.loc else None)
        end_lon = t.last_lon if t.last_lon is not None else (self.s.loc.lon if self.s.loc else None)
        end_odo = t.end_odometer if t.end_odometer is not None else self.s.odometer
        self._set_parked(end_lat, end_lon, end_odo, end_time)
        displacement = 0.0
        if None not in (t.start_lat, t.start_lon, end_lat, end_lon):
            displacement = haversine_m(t.start_lat, t.start_lon, end_lat, end_lon)
        odo_diff = (end_odo - t.start_odometer) if (end_odo is not None and t.start_odometer is not None) else None
        if (odo_diff is None or odo_diff <= 0) and displacement < self.settings.trip_start_distance_m and t.path_m < 300:
            _LOG.info("%s: liike ilman todellista matkaa ohitettu", self.vehicle.id)
            return None
        if end_time < t.start_time:
            end_time = t.start_time
        trip = {
            "vehicle_id": self.vehicle.id,
            "start_time": t.start_time,
            "end_time": end_time,
            "start_lat": t.start_lat,
            "start_lon": t.start_lon,
            "end_lat": end_lat,
            "end_lon": end_lon,
            "start_odometer": t.start_odometer,
            "end_odometer": end_odo,
            "distance_km": round(odo_diff, 1) if odo_diff is not None else round(t.path_m / 1000, 1),
            "gps_distance_km": round(t.path_m / 1000, 2),
            "start_soc": t.start_soc,
            "end_soc": t.end_soc,
            "charging_stops": [
                {
                    "start_time": s.get("start_time"),
                    "end_time": s.get("end_time"),
                    "lat": s.get("lat"),
                    "lon": s.get("lon"),
                    "soc_start": s.get("soc_start"),
                    "soc_end": s.get("soc_end"),
                    "method": s.get("method"),
                }
                for s in t.charging_stops
            ],
        }
        if self.on_trip_finished:
            self.on_trip_finished(trip)
        return trip


def _secs(a: str, b: str) -> float:
    """Sekunnit ISO-aikaleimasta a aikaleimaan b."""
    return (timeutil.parse_iso(b) - timeutil.parse_iso(a)).total_seconds()


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return None if math.isnan(f) else f


def _state_from_dict(raw: dict) -> MachineState:
    s = MachineState(**{k: v for k, v in raw.items() if k not in ("loc", "segment", "trip")})
    if raw.get("loc"):
        s.loc = Point(**raw["loc"])
    if raw.get("segment"):
        s.segment = Segment(**raw["segment"])
    if raw.get("trip"):
        s.trip = ActiveTrip(**raw["trip"])
    return s


def event_from_ha_state(role: str, state_obj: dict) -> Event:
    """Muuntaa HA:n tilaobjektin (get_states tai historia) tapahtumaksi."""
    ts = state_obj.get("last_updated") or state_obj.get("last_changed")
    if isinstance(ts, (int, float)):
        dt = timeutil.from_ts(ts)
    elif ts:
        dt = timeutil.parse_iso(ts)
    else:
        dt = timeutil.now_utc()
    return Event(role=role, state=str(state_obj.get("state", "")), attributes=state_obj.get("attributes") or {}, ts=dt)


__all__ = [
    "TripManager",
    "Event",
    "Zone",
    "haversine_m",
    "STATUS_LABELS",
    "IDLE",
    "DRIVING",
    "PAUSED_CHARGING",
    "NOT_CONFIGURED",
    "event_from_ha_state",
    "timedelta",
]
