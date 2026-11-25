"""Kululaskennan tilakone (lataukset ja tankkaukset), yksi instanssi per auto.

Kirjaa vain määriä (kWh ja litraa). Euroja ei lasketa; yksikköhinnan voi lisätä itse.

Kuten TripManager, tämä on puhdas: se saa HA:n tapahtumat aikaleimoineen ja kutsun
tick(now), eikä lue kelloa itse. Sama logiikka toimii live-datalla, historiapaikkauksessa
ja testeissä.

Lataus (mitattu BMW 530e:n datasta 11/2026):
* kytkentä = auton latausmenetelmä (esim. AC_TYPE2PLUG) ... NOCHARGING. BMW ilmoittaa
  kytkennän viiveellä, joskus vasta ~40 min laturin jälkeen;
* kotilaturin istuntolaskuri (esim. ABB Terra AC) kasvaa koko
  kytkennän ajan ja nollautuu, kun kaapeli irrotetaan. Laskurin maksimi on istunnon
  energia seinästä. Laskurin nollautuminen kesken
  kytkennän aloittaa uuden osan, jonka energia lisätään;
* jos auto lähtee liikkeelle kytkentätiedon jäädessä päälle (BMW ei aina ilmoita irrotusta),
  kytkentä päätetään siirtymästä. Seuraava lataus tunnistetaan silloin auton lataustilasta
  (CHARGINGACTIVE), koska latausmenetelmä ei muutu eikä siitä tule uutta tapahtumaa;
* muualla ladattaessa energia arvioidaan varaustason noususta (kWh / %), ja rivi
  merkitään arvioksi.

Polttoaine (PHEV ja ICE):
* tankin litramäärä tulee kokonaislukuina. Tankkaus = nousu vähintään refuel_min_l
  litraa. Peräkkäiset nousut tunnin sisällä ovat sama tankkaus.
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

INVALID_STATES = {"unavailable", "unknown", "none", ""}
UNPLUGGED = {"NOCHARGING", "NOT_CONNECTED", "DISCONNECTED", "NO_PLUG", "INVALID"}
RESET_DROP_KWH = 0.05  # laskurin pudotus tätä enemmän = uusi laturin istunto
ZERO_KWH = 0.05
PRE_PLUG_WINDOW_S = 3600  # laturi voi alkaa laskea enintään tunnin ennen kuin auto ilmoittaa kytkennän
MOVE_AWAY_M = 300  # kytketty auto siirtyi: kytkentä on päättynyt, vaikka tieto puuttuu
REFUEL_MERGE_S = 3600
CHARGING_ACTIVE = {"CHARGINGACTIVE", "CHARGING"}
MIN_CHARGE_KWH = 0.1


def _float(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _secs(a: str, b: str) -> float:
    return (timeutil.parse_iso(b) - timeutil.parse_iso(a)).total_seconds()


@dataclass
class Plug:
    start: str
    method: str
    lat: float | None
    lon: float | None
    at_home: bool
    soc_start: float | None
    soc_max: float | None
    soc_last: float | None
    segments: list = field(default_factory=list)  # [[base_kwh, max_kwh], ...]


@dataclass
class PendingRefuel:
    before: float
    after: float
    first_ts: str
    last_ts: str
    lat: float | None
    lon: float | None


@dataclass
class ConsumptionState:
    lat: float | None = None
    lon: float | None = None
    soc: float | None = None
    method: str = ""
    plug: Plug | None = None
    counter: float | None = None
    counter_rise_ts: str | None = None  # hetki, jolloin laskuri lähti nollasta
    fuel: float | None = None
    refuel: PendingRefuel | None = None
    last_ts: str | None = None


def charger_kwh(state: str, attributes: dict) -> float | None:
    v = _float(state)
    if v is None:
        return None
    unit = str((attributes or {}).get("unit_of_measurement") or "").strip()
    if unit == "Wh":
        return v / 1000.0
    if unit == "MWh":
        return v * 1000.0
    return v


class ConsumptionTracker:
    def __init__(
        self,
        vehicle: VehicleConfig,
        settings: Settings,
        on_charge: Callable[[dict], Any] | None = None,
        on_refuel: Callable[[dict], Any] | None = None,
        on_fuel_reading: Callable[[str, float], Any] | None = None,
        state_path: str | Path | None = None,
        zones: list | None = None,
    ):
        self.vehicle = vehicle
        self.settings = settings
        self.on_charge = on_charge
        self.on_refuel = on_refuel
        self.on_fuel_reading = on_fuel_reading
        self.state_path = Path(state_path) if state_path else None
        self.zones = zones if zones is not None else []
        self.s = ConsumptionState()
        self._load()

    # ------------------------------------------------------------- pysyvyys
    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            plug = raw.pop("plug", None)
            refuel = raw.pop("refuel", None)
            self.s = ConsumptionState(**raw)
            self.s.plug = Plug(**plug) if plug else None
            self.s.refuel = PendingRefuel(**refuel) if refuel else None
        except Exception:  # pragma: no cover - vioittunut tiedosto
            _LOG.exception("%s: kululaskennan tilaa ei voitu lukea", self.vehicle.id)

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self.s), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @property
    def last_ts(self) -> str | None:
        return self.s.last_ts

    def snapshot(self) -> dict:
        p = self.s.plug
        return {
            "plugged": p is not None,
            "plug_start": p.start if p else None,
            "plug_kwh": round(self._measured(p), 2) if p and p.segments else None,
            "fuel_l": self.s.fuel,
            "charger_kwh": self.s.counter,
        }

    def in_home_zone(self, lat, lon) -> bool:
        return any(z.contains(lat, lon) for z in self.zones)

    # ------------------------------------------------------------ tapahtumat
    def process(self, role: str, state: str, attributes: dict, ts: datetime) -> None:
        state = (state or "").strip()
        ts_iso = timeutil.to_iso(ts)
        if self.s.last_ts and ts_iso < self.s.last_ts:
            ts_iso = self.s.last_ts
        self.tick(timeutil.parse_iso(ts_iso))
        changed = False
        if role == "location":
            changed = self._on_location(attributes or {}, ts_iso)
        elif state.lower() in INVALID_STATES:
            return
        elif role == "soc":
            v = _float(state)
            if v is not None and v != self.s.soc:
                self.s.soc = v
                if self.s.plug:
                    self.s.plug.soc_last = v
                    self.s.plug.soc_max = v if self.s.plug.soc_max is None else max(self.s.plug.soc_max, v)
                changed = True
        elif role == "charging_method":
            changed = self._on_method(state, ts_iso)
        elif role == "charging_state":
            changed = self._on_charging_state(state, ts_iso)
        elif role == "charger_energy":
            changed = self._on_counter(charger_kwh(state, attributes), ts_iso)
        elif role == "fuel_level":
            changed = self._on_fuel(_float(state), ts_iso)
        if changed:
            self.s.last_ts = ts_iso
            self.save()

    def tick(self, now: datetime) -> None:
        r = self.s.refuel
        if r and (now - timeutil.parse_iso(r.last_ts)).total_seconds() >= REFUEL_MERGE_S:
            self._finish_refuel()
            self.save()

    # ---------------------------------------------------------------- sijainti
    def _on_location(self, attrs: dict, ts: str) -> bool:
        lat, lon = _float(attrs.get("latitude")), _float(attrs.get("longitude"))
        if lat is None or lon is None or (lat, lon) == (self.s.lat, self.s.lon):
            return False
        self.s.lat, self.s.lon = lat, lon
        p = self.s.plug
        if p and p.lat is not None and _dist_m(p.lat, p.lon, lat, lon) > MOVE_AWAY_M:
            # Irrotustieto puuttuu, mutta auto on jo liikkeellä: kytkentä on päättynyt
            self._finish_plug(ts, note="kytkentä päätetty, kun auto siirtyi")
        elif p and p.lat is None:
            p.lat, p.lon = lat, lon
            p.at_home = self.in_home_zone(lat, lon)
        return True

    # ----------------------------------------------------------------- lataus
    def _on_method(self, state: str, ts: str) -> bool:
        state = state.upper()
        if state == self.s.method:
            return False
        self.s.method = state
        plugged = state not in UNPLUGGED
        if plugged and self.s.plug is None:
            self._start_plug(state, ts)
        elif not plugged and self.s.plug is not None:
            self._finish_plug(ts)
        elif plugged and self.s.plug is not None:
            self.s.plug.method = state
        return True

    def _on_charging_state(self, state: str, ts: str) -> bool:
        """Lataus alkoi ilman uutta kytkentätietoa: aloitetaan kytkentä lataustilasta.

        Tapahtuu, kun edellinen kytkentä päätettiin auton siirryttyä, mutta auton
        latausmenetelmä jäi samaksi (esim. AC_TYPE2PLUG koko päivän).
        """
        if self.s.plug is not None:
            return False
        if state.upper().replace("_", "").replace(" ", "") not in CHARGING_ACTIVE:
            return False
        method = self.s.method if self.s.method and self.s.method not in UNPLUGGED else "AC"
        _LOG.info("%s: lataus alkoi ilman kytkentätietoa, kytkentä aloitetaan lataustilasta", self.vehicle.id)
        self._start_plug(method, ts)
        return True

    def _start_plug(self, method: str, ts: str) -> None:
        segments = []
        c = self.s.counter
        if c is not None:
            recent_rise = self.s.counter_rise_ts is not None and 0 <= _secs(self.s.counter_rise_ts, ts) <= PRE_PLUG_WINDOW_S
            # Laturi alkoi laskea juuri ennen kuin auto ilmoitti kytkennän: koko istunto kuuluu tälle kytkennälle
            base = 0.0 if (c <= ZERO_KWH or recent_rise) else c
            segments.append([base, c])
        self.s.plug = Plug(
            start=ts,
            method=method,
            lat=self.s.lat,
            lon=self.s.lon,
            at_home=self.in_home_zone(self.s.lat, self.s.lon),
            soc_start=self.s.soc,
            soc_max=self.s.soc,
            soc_last=self.s.soc,
            segments=segments,
        )

    def _on_counter(self, value: float | None, ts: str) -> bool:
        if value is None or value < 0:
            return False
        prev = self.s.counter
        if prev is not None and abs(value - prev) < 1e-9:
            return False
        reset = prev is not None and value < prev - RESET_DROP_KWH
        if (prev is None or prev <= ZERO_KWH or reset) and value > ZERO_KWH:
            self.s.counter_rise_ts = ts
        elif value <= ZERO_KWH:
            self.s.counter_rise_ts = None
        self.s.counter = value
        p = self.s.plug
        if p is not None:
            if reset or not p.segments:
                base = 0.0 if (reset or prev is None or prev <= ZERO_KWH) else prev
                p.segments.append([base, value])
            else:
                p.segments[-1][1] = max(p.segments[-1][1], value)
        return True

    @staticmethod
    def _measured(p: Plug) -> float:
        return sum(max(0.0, mx - base) for base, mx in p.segments)

    def _finish_plug(self, ts: str, note: str = "") -> None:
        p = self.s.plug
        self.s.plug = None
        if p is None:
            return
        has_charger = bool(self.vehicle.entity("charger_energy"))
        soc_gain = None
        if p.soc_start is not None and p.soc_max is not None:
            soc_gain = max(0.0, p.soc_max - p.soc_start)
        measured = self._measured(p) if (has_charger and p.at_home and p.segments) else None
        if measured is not None and self.vehicle.charger_shared and soc_gain is not None and soc_gain <= 0:
            # Sama kotilaturi on usealla autolla: energia kuuluu autolle, jonka varaus nousi
            _LOG.info("%s: kytkentä %s ilman varauksen nousua, laturin energia jätetään toiselle autolle", self.vehicle.id, p.start)
            measured = None
        estimate = None
        if soc_gain and self.vehicle.charge_kwh_per_pct > 0:
            estimate = round(soc_gain * self.vehicle.charge_kwh_per_pct, 2)
        if measured is not None and measured >= MIN_CHARGE_KWH:
            qty, source = round(measured, 2), "charger"
        elif estimate is not None and estimate >= MIN_CHARGE_KWH and not (has_charger and p.at_home and measured is not None):
            qty, source = estimate, "estimate"
        else:
            if soc_gain:
                _LOG.info("%s: kytkentä %s ilman mitattua energiaa (varaus +%.0f %%)", self.vehicle.id, p.start, soc_gain)
            return
        event = {
            "kind": "charge",
            "start_time": p.start,
            "end_time": ts,
            "lat": p.lat,
            "lon": p.lon,
            "at_home": int(bool(p.at_home)),
            "method": p.method,
            "soc_start": p.soc_start,
            "soc_end": p.soc_last if p.soc_last is not None else p.soc_max,
            "quantity": qty,
            "unit": "kWh",
            "source": source,
            "estimated_kwh": estimate,
            "note": note,
        }
        _LOG.info("%s: lataus %s %.2f kWh (%s)", self.vehicle.id, p.start, qty, source)
        if self.on_charge:
            self.on_charge(event)

    # ------------------------------------------------------------ polttoaine
    def _on_fuel(self, litres: float | None, ts: str) -> bool:
        if litres is None or litres < 0 or not self.vehicle.has_fuel:
            return False
        prev = self.s.fuel
        if prev is not None and abs(litres - prev) < 1e-9:
            return False
        self.s.fuel = litres
        if self.on_fuel_reading:
            self.on_fuel_reading(ts, litres)
        if prev is None:
            return True
        r = self.s.refuel
        if litres > prev:
            if r and _secs(r.last_ts, ts) <= REFUEL_MERGE_S:
                r.after, r.last_ts = litres, ts
            else:
                if r:
                    self._finish_refuel()
                self.s.refuel = PendingRefuel(prev, litres, ts, ts, self.s.lat, self.s.lon)
        elif r:
            self._finish_refuel()
        return True

    def _finish_refuel(self) -> None:
        r = self.s.refuel
        self.s.refuel = None
        if r is None:
            return
        qty = round(r.after - r.before, 1)
        if qty < self.settings.refuel_min_l:
            return
        event = {
            "kind": "refuel",
            "start_time": r.first_ts,
            "end_time": r.last_ts,
            "lat": r.lat,
            "lon": r.lon,
            "at_home": int(self.in_home_zone(r.lat, r.lon)),
            "fuel_before": r.before,
            "fuel_after": r.after,
            "quantity": qty,
            "unit": "l",
            "source": "tank_level",
        }
        _LOG.info("%s: tankkaus %s %.0f l", self.vehicle.id, r.first_ts, qty)
        if self.on_refuel:
            self.on_refuel(event)


def _overlaps(ev: dict, other: dict) -> bool:
    a0, a1 = ev["start_time"], ev.get("end_time") or ev["start_time"]
    b0, b1 = other["start_time"], other.get("end_time") or other["start_time"]
    return a0 <= b1 and b0 <= a1


def recover_charges(
    vehicle: VehicleConfig,
    settings: Settings,
    events: list[tuple[datetime, str, str, dict]],
    window_start: datetime,
    existing: list[dict],
    zones: list | None = None,
) -> list[dict]:
    """Käy HA:n historian uudelleen läpi ja palauttaa lataukset, jotka puuttuvat kirjanpidosta.

    events: (aika, rooli, tila, attribuutit) aikajärjestyksessä. Lataus, joka oli jo kesken
    ikkunan alussa, ohitetaan (sen alkua ei tiedetä). Lataus ohitetaan myös, jos se menee
    ajallisesti päällekkäin minkä tahansa jo kirjatun latauksen kanssa.
    """
    found: list[dict] = []
    ct = ConsumptionTracker(vehicle, settings, on_charge=found.append, zones=zones)
    for ts, role, state, attrs in events:
        ct.process(role, state, attrs, ts)
    cutoff = timeutil.to_iso(window_start + timedelta(seconds=1))
    charges = [e for e in existing if e.get("kind") == "charge"]
    out = []
    for ev in found:
        if ev["start_time"] <= cutoff:
            continue
        if any(_overlaps(ev, old) for old in charges):
            continue
        ev["note"] = "palautettu Home Assistantin historiasta"
        out.append(ev)
        charges.append(ev)
    return out


__all__ = ["ConsumptionTracker", "charger_kwh", "recover_charges"]
