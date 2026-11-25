"""Autokohtainen SQLite-tietokanta.

Jokaisella autolla on oma tiedosto (data/<vehicle_id>/logbook.db). Tiedosto tietää,
mille autolle se kuuluu (meta-taulu ja trips.vehicle_id CHECK), joten eri autojen
matkoja ei voi sekoittaa edes kyselyvirheellä.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from . import timeutil
from .vehicle_registry import VEHICLE_ID_RE, VehicleConfig

TRIP_TYPES = ("business", "private", "commute")
TRIP_TYPE_LABELS = {"business": "Työajo", "private": "Yksityisajo", "commute": "Työmatka"}
TRIP_TYPE_BY_LABEL = {v.lower(): k for k, v in TRIP_TYPE_LABELS.items()}

# Autosta mitatut kentät: muutos vaatii perustelun, alkuperäinen arvo säilyy orig_*-sarakkeessa
MEASURED_FIELDS = (
    "start_time",
    "end_time",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "start_odometer",
    "end_odometer",
)
ADDRESS_FIELDS = ("start_address", "end_address")
CORRECTION_FIELDS = MEASURED_FIELDS + ADDRESS_FIELDS
FREE_FIELDS = ("trip_type", "purpose", "comment")
EDITABLE_FIELDS = CORRECTION_FIELDS + FREE_FIELDS
NUMERIC_FIELDS = ("start_lat", "start_lon", "end_lat", "end_lon", "start_odometer", "end_odometer")

CHANGE_SOURCES = (
    "ui",
    "excel_drive",
    "excel_upload",
    "merge",
    "unmerge",
    "manual_add",
    "gap_fill",
    "period",
    "vehicle_info",
    "consumption",
)

CONSUMPTION_KINDS = {"charge": "Lataus", "refuel": "Tankkaus"}
CONSUMPTION_SOURCES = {
    "charger": "mitattu laturista",
    "estimate": "arvio varaustasosta",
    "tank_level": "tankin mittarista",
    "manual": "käsin kirjattu",
}
CONSUMPTION_UNITS = {"charge": "kWh", "refuel": "l"}


class LogbookError(Exception):
    """Käyttäjälle näytettävä virhe."""


class PeriodLockedError(LogbookError):
    pass


def _schema(vehicle_id: str) -> str:
    # vehicle_id on tarkistettu säännöllisellä lausekkeella, joten sen voi upottaa CHECK-ehtoon
    return f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS vehicle_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL CHECK (vehicle_id = '{vehicle_id}'),
    vin TEXT, make TEXT, model TEXT, powertrain TEXT, plate TEXT, driver TEXT,
    valid_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL CHECK (vehicle_id = '{vehicle_id}'),
    start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    start_lat REAL, start_lon REAL, start_address TEXT DEFAULT '',
    end_lat REAL, end_lon REAL, end_address TEXT DEFAULT '',
    start_odometer REAL, end_odometer REAL, distance_km REAL,
    start_soc REAL, end_soc REAL,
    trip_type TEXT NOT NULL DEFAULT 'private' CHECK (trip_type IN ('business','private','commute')),
    purpose TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    comment_updated_at TEXT, comment_source TEXT,
    source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','manual','gap_fill','merge')),
    merged_into_id INTEGER DEFAULT NULL REFERENCES trips(id),
    is_merged_parent INTEGER NOT NULL DEFAULT 0,
    corrected INTEGER NOT NULL DEFAULT 0,
    gps_distance_km REAL,
    orig_start_time TEXT, orig_end_time TEXT,
    orig_start_lat REAL, orig_start_lon REAL, orig_end_lat REAL, orig_end_lon REAL,
    orig_start_odometer REAL, orig_end_odometer REAL,
    orig_start_address TEXT, orig_end_address TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trips_start ON trips(start_time);
CREATE INDEX IF NOT EXISTS idx_trips_merged ON trips(merged_into_id);
CREATE TABLE IF NOT EXISTS trip_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER,
    changed_at TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_trip ON trip_changes(trip_id);
CREATE TRIGGER IF NOT EXISTS trip_changes_no_update BEFORE UPDATE ON trip_changes
BEGIN SELECT RAISE(ABORT, 'trip_changes on vain lisäys -taulu'); END;
CREATE TRIGGER IF NOT EXISTS trip_changes_no_delete BEFORE DELETE ON trip_changes
BEGIN SELECT RAISE(ABORT, 'trip_changes on vain lisäys -taulu'); END;
CREATE TABLE IF NOT EXISTS periods (
    year INTEGER NOT NULL, month INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','locked')),
    locked_at TEXT, report_sha256 TEXT, content_sha256 TEXT, archive_path TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    odometer_start REAL, odometer_end REAL,
    PRIMARY KEY (year, month)
);
CREATE TABLE IF NOT EXISTS charging_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    start_time TEXT, end_time TEXT, lat REAL, lon REAL, address TEXT DEFAULT '',
    soc_start REAL, soc_end REAL, method TEXT
);
CREATE TABLE IF NOT EXISTS gap_acks (
    prev_trip_id INTEGER NOT NULL, next_trip_id INTEGER NOT NULL,
    reason TEXT NOT NULL, acked_at TEXT NOT NULL,
    PRIMARY KEY (prev_trip_id, next_trip_id)
);
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL, source TEXT NOT NULL, filename TEXT,
    status TEXT NOT NULL, changed_rows INTEGER NOT NULL DEFAULT 0, summary TEXT
);
CREATE TABLE IF NOT EXISTS import_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, trip_id INTEGER NOT NULL, field TEXT NOT NULL,
    ui_value TEXT, excel_value TEXT, filename TEXT, source TEXT,
    resolved_at TEXT, resolution TEXT
);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS consumption_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL CHECK (vehicle_id = '{vehicle_id}'),
    kind TEXT NOT NULL CHECK (kind IN ('charge','refuel')),
    start_time TEXT NOT NULL, end_time TEXT,
    lat REAL, lon REAL, address TEXT NOT NULL DEFAULT '', at_home INTEGER NOT NULL DEFAULT 0,
    method TEXT, soc_start REAL, soc_end REAL, fuel_before REAL, fuel_after REAL,
    quantity REAL NOT NULL, unit TEXT NOT NULL CHECK (unit IN ('kWh','l')),
    source TEXT NOT NULL CHECK (source IN ('charger','estimate','tank_level','manual')),
    orig_quantity REAL, estimated_kwh REAL,
    unit_price REAL,
    comment TEXT NOT NULL DEFAULT '',
    corrected INTEGER NOT NULL DEFAULT 0,
    voided INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_consumption_start ON consumption_events(start_time);
CREATE TABLE IF NOT EXISTS fuel_readings (ts TEXT PRIMARY KEY, litres REAL NOT NULL);
"""


def _s(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class VehicleDB:
    """Yhden auton tietokanta. Kaikki kyselyt koskevat vain tätä autoa."""

    def __init__(self, path: str | Path, vehicle_id: str):
        if not VEHICLE_ID_RE.match(vehicle_id):
            raise ValueError(f"Virheellinen auton tunniste: {vehicle_id}")
        self.vehicle_id = vehicle_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_schema(vehicle_id))
        self._check_owner()

    # ------------------------------------------------------------------ perus
    def _check_owner(self) -> None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='vehicle_id'").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES ('vehicle_id', ?)", (self.vehicle_id,))
        elif row["value"] != self.vehicle_id:
            raise LogbookError(
                f"Tietokanta {self.path} kuuluu autolle '{row['value']}', ei autolle '{self.vehicle_id}'."
            )

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM app_state WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ----------------------------------------------------------- autotiedot
    def sync_vehicle_info(self, cfg: VehicleConfig) -> bool:
        """Tallentaa asetussivun autotiedot. Uusi rivi vain, jos jokin muuttui."""
        if cfg.id != self.vehicle_id:
            raise LogbookError("Autotiedot kuuluvat toiselle autolle.")
        current = self.query("SELECT * FROM vehicle_info ORDER BY id DESC LIMIT 1")
        new = {
            "vin": cfg.vin,
            "make": cfg.make,
            "model": cfg.model,
            "powertrain": cfg.powertrain,
            "plate": cfg.plate,
            "driver": cfg.driver,
        }
        if current and all((current[0].get(k) or "") == (v or "") for k, v in new.items()):
            return False
        now = timeutil.now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT INTO vehicle_info(vehicle_id, vin, make, model, powertrain, plate, driver, valid_from)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (self.vehicle_id, cfg.vin, cfg.make, cfg.model, cfg.powertrain, cfg.plate, cfg.driver, now),
            )
            if current:
                for k, v in new.items():
                    if (current[0].get(k) or "") != (v or ""):
                        self._log(c, None, f"vehicle_info.{k}", current[0].get(k), v, "vehicle_info", "Asetussivu")
        return True

    def vehicle_info_history(self) -> list[dict]:
        return self.query("SELECT * FROM vehicle_info ORDER BY valid_from, id")

    def vehicle_info_at(self, when_iso: str | None) -> dict:
        rows = self.vehicle_info_history()
        if not rows:
            return {}
        chosen = rows[0]
        for r in rows:
            if when_iso is None or r["valid_from"] <= when_iso:
                chosen = r
        return chosen

    # ---------------------------------------------------------------- muutosloki
    def _log(self, c, trip_id, field, old, new, source, reason=None, at=None) -> None:
        c.execute(
            "INSERT INTO trip_changes(trip_id, changed_at, field, old_value, new_value, source, reason)"
            " VALUES (?,?,?,?,?,?,?)",
            (trip_id, at or timeutil.now_iso(), field, _s(old), _s(new), source, reason),
        )

    def history(self, trip_id: int | None = None) -> list[dict]:
        if trip_id is None:
            return self.query("SELECT * FROM trip_changes ORDER BY id")
        ids = [trip_id] + self.descendants(trip_id)
        marks = ",".join("?" * len(ids))
        return self.query(f"SELECT * FROM trip_changes WHERE trip_id IN ({marks}) ORDER BY id", ids)

    def changes_between(self, start_iso: str, end_iso: str) -> list[dict]:
        return self.query(
            "SELECT * FROM trip_changes WHERE changed_at >= ? AND changed_at < ? ORDER BY id",
            (start_iso, end_iso),
        )

    # ------------------------------------------------------------------ jaksot
    def period(self, year: int, month: int) -> dict:
        rows = self.query("SELECT * FROM periods WHERE year=? AND month=?", (year, month))
        return rows[0] if rows else {"year": year, "month": month, "status": "open", "version": 0}

    def periods(self) -> list[dict]:
        return self.query("SELECT * FROM periods ORDER BY year, month")

    def is_locked(self, year: int, month: int) -> bool:
        return self.period(year, month)["status"] == "locked"

    def trip_is_locked(self, trip: dict) -> bool:
        y, m = timeutil.local_year_month(trip["start_time"])
        return self.is_locked(y, m)

    def _assert_open(self, trip: dict) -> None:
        if self.trip_is_locked(trip):
            y, m = timeutil.local_year_month(trip["start_time"])
            raise PeriodLockedError(f"Jakso {m:02d}/{y} on lukittu. Matkaa ei voi muuttaa.")

    def set_period_locked(self, year: int, month: int, **fields) -> None:
        p = self.period(year, month)
        with self.tx() as c:
            c.execute(
                "INSERT INTO periods(year, month, status, locked_at, report_sha256, content_sha256, archive_path,"
                " version, odometer_start, odometer_end) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(year, month) DO UPDATE SET status=excluded.status, locked_at=excluded.locked_at,"
                " report_sha256=excluded.report_sha256, content_sha256=excluded.content_sha256,"
                " archive_path=excluded.archive_path, version=excluded.version,"
                " odometer_start=excluded.odometer_start, odometer_end=excluded.odometer_end",
                (
                    year,
                    month,
                    "locked",
                    fields.get("locked_at") or timeutil.now_iso(),
                    fields.get("report_sha256"),
                    fields.get("content_sha256"),
                    fields.get("archive_path"),
                    fields.get("version", p.get("version", 0) + 1),
                    fields.get("odometer_start"),
                    fields.get("odometer_end"),
                ),
            )
            self._log(c, None, "period_lock", f"{year}-{month:02d} open", "locked", "period", fields.get("reason"))

    def unlock_period(self, year: int, month: int, reason: str) -> None:
        if not reason or not reason.strip():
            raise LogbookError("Lukituksen avaaminen vaatii perustelun.")
        if not self.is_locked(year, month):
            raise LogbookError("Jakso ei ole lukittu.")
        with self.tx() as c:
            c.execute("UPDATE periods SET status='open' WHERE year=? AND month=?", (year, month))
            self._log(c, None, "period_lock", f"{year}-{month:02d} locked", "open", "period", reason.strip())

    # ------------------------------------------------------------------ matkat
    def insert_trip(self, trip: dict, source: str = "auto", reason: str | None = None) -> int:
        """Tallentaa uuden matkan. Autosta tulleet arvot kopioidaan myös orig_*-sarakkeisiin."""
        if trip.get("vehicle_id", self.vehicle_id) != self.vehicle_id:
            raise LogbookError("Matka kuuluu toiselle autolle.")
        if source not in ("auto", "manual", "gap_fill"):
            raise ValueError(source)
        start_odo, end_odo = trip.get("start_odometer"), trip.get("end_odometer")
        distance = trip.get("distance_km")
        if distance is None and start_odo is not None and end_odo is not None:
            distance = round(float(end_odo) - float(start_odo), 1)
        y, m = timeutil.local_year_month(trip["start_time"])
        if source != "auto" and self.is_locked(y, m):
            raise PeriodLockedError(f"Jakso {m:02d}/{y} on lukittu. Ajoa ei voi lisätä.")
        trip_type = trip.get("trip_type") or "private"
        if trip_type not in TRIP_TYPES:
            raise LogbookError(f"Tuntematon ajotyyppi {trip_type}")
        now = timeutil.now_iso()
        cols = {
            "vehicle_id": self.vehicle_id,
            "start_time": trip["start_time"],
            "end_time": trip["end_time"],
            "start_lat": trip.get("start_lat"),
            "start_lon": trip.get("start_lon"),
            "start_address": trip.get("start_address") or "",
            "end_lat": trip.get("end_lat"),
            "end_lon": trip.get("end_lon"),
            "end_address": trip.get("end_address") or "",
            "start_odometer": start_odo,
            "end_odometer": end_odo,
            "distance_km": distance,
            "start_soc": trip.get("start_soc"),
            "end_soc": trip.get("end_soc"),
            "trip_type": trip_type,
            "purpose": trip.get("purpose") or "",
            "comment": trip.get("comment") or "",
            "source": source,
            "gps_distance_km": trip.get("gps_distance_km"),
            "created_at": now,
            "updated_at": now,
        }
        for f in MEASURED_FIELDS + ADDRESS_FIELDS:
            cols[f"orig_{f}"] = cols.get(f)
        marks = ",".join("?" * len(cols))
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO trips({','.join(cols)}) VALUES ({marks})", list(cols.values()))
            trip_id = cur.lastrowid
            if source in ("manual", "gap_fill"):
                summary = (
                    f"{trip['start_time']} – {trip['end_time']}, {_s(start_odo)} – {_s(end_odo)} km"
                )
                self._log(c, trip_id, "trip", None, summary, "manual_add" if source == "manual" else "gap_fill", reason)
            for stop in trip.get("charging_stops") or []:
                c.execute(
                    "INSERT INTO charging_stops(trip_id, start_time, end_time, lat, lon, address, soc_start, soc_end,"
                    " method) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        trip_id,
                        stop.get("start_time"),
                        stop.get("end_time"),
                        stop.get("lat"),
                        stop.get("lon"),
                        stop.get("address") or "",
                        stop.get("soc_start"),
                        stop.get("soc_end"),
                        stop.get("method"),
                    ),
                )
        return trip_id

    def get_trip(self, trip_id: int) -> dict | None:
        rows = self.query("SELECT * FROM trips WHERE id=?", (trip_id,))
        return rows[0] if rows else None

    def require_trip(self, trip_id: int) -> dict:
        trip = self.get_trip(trip_id)
        if trip is None:
            raise LogbookError(f"Matkaa {trip_id} ei ole tämän auton ({self.vehicle_id}) ajopäiväkirjassa.")
        return trip

    def list_trips(
        self,
        start_iso: str | None = None,
        end_iso: str | None = None,
        trip_types: Iterable[str] | None = None,
        include_merged: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM trips WHERE 1=1"
        params: list[Any] = []
        if not include_merged:
            sql += " AND merged_into_id IS NULL"
        if start_iso:
            sql += " AND start_time >= ?"
            params.append(start_iso)
        if end_iso:
            sql += " AND start_time < ?"
            params.append(end_iso)
        if trip_types:
            types = list(trip_types)
            sql += f" AND trip_type IN ({','.join('?' * len(types))})"
            params += types
        sql += " ORDER BY start_time, id"
        return self.query(sql, params)

    def last_trip(self) -> dict | None:
        rows = self.query("SELECT * FROM trips WHERE merged_into_id IS NULL ORDER BY end_time DESC, id DESC LIMIT 1")
        return rows[0] if rows else None

    def children(self, trip_id: int) -> list[dict]:
        return self.query("SELECT * FROM trips WHERE merged_into_id=? ORDER BY start_time, id", (trip_id,))

    def descendants(self, trip_id: int) -> list[int]:
        out: list[int] = []
        stack = [trip_id]
        while stack:
            tid = stack.pop()
            for ch in self.children(tid):
                out.append(ch["id"])
                stack.append(ch["id"])
        return out

    def active_root(self, trip_id: int) -> dict | None:
        """Palauttaa näkyvän matkan, johon (mahdollisesti yhdistetty) matka nykyisin kuuluu."""
        trip = self.get_trip(trip_id)
        seen = set()
        while trip and trip["merged_into_id"] is not None and trip["id"] not in seen:
            seen.add(trip["id"])
            trip = self.get_trip(trip["merged_into_id"])
        return trip

    def charging_stops(self, trip_id: int) -> list[dict]:
        ids = [trip_id] + self.descendants(trip_id)
        marks = ",".join("?" * len(ids))
        return self.query(f"SELECT * FROM charging_stops WHERE trip_id IN ({marks}) ORDER BY start_time", ids)

    def set_geocoded_address(self, trip_id: int, which: str, address: str) -> None:
        """Täyttää osoitteen taustahausta. Ei ylikirjoita käyttäjän korjaamaa osoitetta."""
        if which not in ADDRESS_FIELDS:
            raise ValueError(which)
        with self.tx() as c:
            c.execute(
                f"UPDATE trips SET {which}=?, orig_{which}=COALESCE(NULLIF(orig_{which}, ''), ?)"
                f" WHERE id=? AND ({which} IS NULL OR {which}='')",
                (address, address, trip_id),
            )

    def set_stop_address(self, stop_id: int, address: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE charging_stops SET address=? WHERE id=? AND (address IS NULL OR address='')", (address, stop_id))

    def update_field(
        self, trip_id: int, field: str, value: Any, source: str = "ui", reason: str | None = None
    ) -> dict:
        """Muuttaa yhtä kenttää ja kirjaa muutoksen muutoslokiin."""
        if field not in EDITABLE_FIELDS:
            raise LogbookError(f"Kenttää {field} ei voi muokata.")
        if source not in CHANGE_SOURCES:
            raise ValueError(source)
        trip = self.require_trip(trip_id)
        if trip["merged_into_id"] is not None:
            raise LogbookError("Matka on yhdistetty toiseen matkaan. Muokkaa yhdistettyä matkaa.")
        locked = self.trip_is_locked(trip)
        if locked and field != "comment":
            self._assert_open(trip)
        if field in NUMERIC_FIELDS:
            value = None if value in (None, "") else float(str(value).replace(",", "."))
        elif field == "trip_type":
            value = str(value)
            if value not in TRIP_TYPES:
                raise LogbookError(f"Tuntematon ajotyyppi {value}")
        elif field in ("start_time", "end_time"):
            value = timeutil.to_iso(timeutil.parse_iso(str(value)))
        else:
            value = "" if value is None else str(value).strip()
        if field in CORRECTION_FIELDS and not (reason or "").strip():
            raise LogbookError("Autosta tulleen tiedon korjaus vaatii perustelun.")
        old = trip[field]
        if _s(old) == _s(value) or (old in (None, "") and value in (None, "")):
            return trip
        now = timeutil.now_iso()
        sets = {field: value, "updated_at": now}
        if field == "comment":
            sets["comment_updated_at"] = now
            sets["comment_source"] = "excel" if source.startswith("excel") else "ui"
        if field in CORRECTION_FIELDS:
            sets["corrected"] = 1
        if field in ("start_odometer", "end_odometer"):
            s = value if field == "start_odometer" else trip["start_odometer"]
            e = value if field == "end_odometer" else trip["end_odometer"]
            if s is not None and e is not None:
                sets["distance_km"] = round(e - s, 1)
        with self.tx() as c:
            assignments = ", ".join(f"{k}=?" for k in sets)
            c.execute(f"UPDATE trips SET {assignments} WHERE id=?", [*sets.values(), trip_id])
            if locked and field == "comment":
                reason = (reason + "; " if reason else "") + "lisätty lukituksen jälkeen"
            self._log(c, trip_id, field, old, value, source, reason)
        return self.require_trip(trip_id)

    # ----------------------------------------------------------- yhdistäminen
    def merge(self, trip_ids: list[int]) -> int:
        ids = sorted(set(int(i) for i in trip_ids))
        if len(ids) < 2:
            raise LogbookError("Valitse vähintään kaksi matkaa yhdistettäväksi.")
        trips = []
        for tid in ids:
            t = self.get_trip(tid)
            if t is None:
                raise LogbookError(
                    f"Matkaa {tid} ei ole auton {self.vehicle_id} ajopäiväkirjassa. Vain saman auton matkoja voi yhdistää."
                )
            if t["merged_into_id"] is not None:
                raise LogbookError(f"Matka {tid} on jo yhdistetty toiseen matkaan.")
            self._assert_open(t)
            trips.append(t)
        trips.sort(key=lambda t: (t["start_time"], t["id"]))
        first, last = trips[0], trips[-1]
        if first["start_odometer"] is not None and last["end_odometer"] is not None:
            distance = round(last["end_odometer"] - first["start_odometer"], 1)
        else:
            distance = round(sum(t["distance_km"] or 0 for t in trips), 1)
        trip_type = "business" if any(t["trip_type"] == "business" for t in trips) else first["trip_type"]
        purposes = []
        for t in trips:
            if t["purpose"] and t["purpose"] not in purposes:
                purposes.append(t["purpose"])
        comments = []
        for t in trips:
            if t["comment"] and t["comment"] not in comments:
                comments.append(t["comment"])
        gps = [t["gps_distance_km"] for t in trips if t["gps_distance_km"] is not None]
        now = timeutil.now_iso()
        row = {
            "vehicle_id": self.vehicle_id,
            "start_time": first["start_time"],
            "end_time": last["end_time"],
            "start_lat": first["start_lat"],
            "start_lon": first["start_lon"],
            "start_address": first["start_address"],
            "end_lat": last["end_lat"],
            "end_lon": last["end_lon"],
            "end_address": last["end_address"],
            "start_odometer": first["start_odometer"],
            "end_odometer": last["end_odometer"],
            "distance_km": distance,
            "start_soc": first["start_soc"],
            "end_soc": last["end_soc"],
            "trip_type": trip_type,
            "purpose": "; ".join(purposes),
            "comment": " | ".join(comments),
            "source": "merge",
            "is_merged_parent": 1,
            "corrected": int(any(t["corrected"] for t in trips)),
            "gps_distance_km": round(sum(gps), 2) if gps else None,
            "created_at": now,
            "updated_at": now,
        }
        for f in MEASURED_FIELDS + ADDRESS_FIELDS:
            src = first if f.startswith("start") else last
            row[f"orig_{f}"] = src[f"orig_{f}"]
        with self.tx() as c:
            cur = c.execute(
                f"INSERT INTO trips({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values())
            )
            new_id = cur.lastrowid
            for t in trips:
                c.execute("UPDATE trips SET merged_into_id=?, updated_at=? WHERE id=?", (new_id, now, t["id"]))
                self._log(c, t["id"], "merged_into_id", None, new_id, "merge")
            self._log(c, new_id, "merge", None, ",".join(str(t["id"]) for t in trips), "merge")
        return new_id

    def unmerge(self, parent_id: int) -> list[int]:
        parent = self.require_trip(parent_id)
        if not parent["is_merged_parent"]:
            raise LogbookError("Matka ei ole yhdistetty matka.")
        if parent["merged_into_id"] is not None:
            raise LogbookError("Pura ensin ylempi yhdistäminen.")
        self._assert_open(parent)
        kids = self.children(parent_id)
        now = timeutil.now_iso()
        with self.tx() as c:
            for k in kids:
                c.execute("UPDATE trips SET merged_into_id=NULL, updated_at=? WHERE id=?", (now, k["id"]))
                self._log(c, k["id"], "merged_into_id", parent_id, None, "unmerge")
            # Yhdistetylle riville tehdyt kommentit eivät katoa: ne siirretään ensimmäiselle osamatkalle
            if kids and parent["comment"]:
                first = kids[0]
                if parent["comment"] not in (first["comment"] or ""):
                    new_comment = " | ".join(x for x in (first["comment"], parent["comment"]) if x)
                    c.execute("UPDATE trips SET comment=?, updated_at=? WHERE id=?", (new_comment, now, first["id"]))
                    self._log(c, first["id"], "comment", first["comment"], new_comment, "unmerge")
            c.execute("UPDATE charging_stops SET trip_id=? WHERE trip_id=?", (kids[0]["id"] if kids else None, parent_id))
            c.execute("DELETE FROM trips WHERE id=?", (parent_id,))
            self._log(c, parent_id, "unmerge", ",".join(str(k["id"]) for k in kids), None, "unmerge")
        return [k["id"] for k in kids]

    # ---------------------------------------------------------- aukkokuittaus
    def gap_acks(self) -> dict[tuple[int, int], dict]:
        return {(r["prev_trip_id"], r["next_trip_id"]): r for r in self.query("SELECT * FROM gap_acks")}

    def ack_gap(self, prev_id: int, next_id: int, reason: str) -> None:
        if not reason or not reason.strip():
            raise LogbookError("Aukon kuittaus vaatii perustelun.")
        nxt = self.require_trip(next_id)
        self.require_trip(prev_id)
        self._assert_open(nxt)
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO gap_acks(prev_trip_id, next_trip_id, reason, acked_at) VALUES (?,?,?,?)",
                (prev_id, next_id, reason.strip(), timeutil.now_iso()),
            )
            self._log(c, next_id, "gap_ack", None, f"{prev_id}->{next_id}", "ui", reason.strip())

    # --------------------------------------------------------------- tuonnit
    def log_import(self, source: str, filename: str, status: str, changed_rows: int, summary: dict) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO import_log(imported_at, source, filename, status, changed_rows, summary)"
                " VALUES (?,?,?,?,?,?)",
                (timeutil.now_iso(), source, filename, status, changed_rows, json.dumps(summary, ensure_ascii=False)),
            )
            return cur.lastrowid

    def imports(self, limit: int = 20) -> list[dict]:
        rows = self.query("SELECT * FROM import_log ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            try:
                r["summary"] = json.loads(r["summary"] or "{}")
            except ValueError:
                r["summary"] = {}
        return rows

    def add_conflict(self, trip_id: int, field: str, ui_value, excel_value, filename: str, source: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO import_conflicts(created_at, trip_id, field, ui_value, excel_value, filename, source)"
                " VALUES (?,?,?,?,?,?,?)",
                (timeutil.now_iso(), trip_id, field, _s(ui_value), _s(excel_value), filename, source),
            )

    def open_conflicts(self) -> list[dict]:
        return self.query("SELECT * FROM import_conflicts WHERE resolved_at IS NULL ORDER BY id")

    def resolve_conflict(self, conflict_id: int, choice: str) -> None:
        rows = self.query("SELECT * FROM import_conflicts WHERE id=? AND resolved_at IS NULL", (conflict_id,))
        if not rows:
            raise LogbookError("Ristiriitaa ei löydy tai se on jo ratkaistu.")
        conf = rows[0]
        if choice not in ("ui", "excel"):
            raise LogbookError("Valitse joko käyttöliittymän tai Excelin arvo.")
        if choice == "excel":
            src = conf["source"] if conf["source"] in CHANGE_SOURCES else "excel_upload"
            self.update_field(conf["trip_id"], conf["field"], conf["excel_value"], source=src, reason="Tuontiristiriita: valittu Excelin arvo")
        with self.tx() as c:
            c.execute(
                "UPDATE import_conflicts SET resolved_at=?, resolution=? WHERE id=?",
                (timeutil.now_iso(), choice, conflict_id),
            )

    # ------------------------------------------------------------ kulut
    def insert_consumption(self, event: dict, reason: str | None = None) -> int:
        """Tallentaa latauksen tai tankkauksen. Käsin lisätty kirjataan muutoslokiin."""
        kind = event.get("kind")
        if kind not in CONSUMPTION_KINDS:
            raise LogbookError(f"Tuntematon kulutyyppi {kind}")
        source = event.get("source") or "manual"
        if source not in CONSUMPTION_SOURCES:
            raise ValueError(source)
        qty = event.get("quantity")
        if qty is None or float(qty) <= 0:
            raise LogbookError("Määrän on oltava suurempi kuin nolla.")
        y, m = timeutil.local_year_month(event["start_time"])
        if source == "manual" and self.is_locked(y, m):
            raise PeriodLockedError(f"Jakso {m:02d}/{y} on lukittu. Kulua ei voi lisätä.")
        price = event.get("unit_price")
        now = timeutil.now_iso()
        cols = {
            "vehicle_id": self.vehicle_id,
            "kind": kind,
            "start_time": event["start_time"],
            "end_time": event.get("end_time") or event["start_time"],
            "lat": event.get("lat"),
            "lon": event.get("lon"),
            "address": event.get("address") or "",
            "at_home": int(bool(event.get("at_home"))),
            "method": event.get("method"),
            "soc_start": event.get("soc_start"),
            "soc_end": event.get("soc_end"),
            "fuel_before": event.get("fuel_before"),
            "fuel_after": event.get("fuel_after"),
            "quantity": round(float(qty), 2),
            "unit": CONSUMPTION_UNITS[kind],
            "source": source,
            "orig_quantity": round(float(qty), 2),
            "estimated_kwh": event.get("estimated_kwh"),
            "unit_price": None if price in (None, "") else float(price),
            "comment": event.get("comment") or event.get("note") or "",
            "created_at": now,
            "updated_at": now,
        }
        with self.tx() as c:
            cur = c.execute(
                f"INSERT INTO consumption_events({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                list(cols.values()),
            )
            eid = cur.lastrowid
            if source == "manual":
                summary = f"{CONSUMPTION_KINDS[kind]} {event['start_time']}, {_s(cols['quantity'])} {cols['unit']}"
                self._log(c, None, f"consumption:{eid}", None, summary, "consumption", reason or "Käsin kirjattu kulu")
        return eid

    def get_consumption(self, event_id: int) -> dict | None:
        rows = self.query("SELECT * FROM consumption_events WHERE id=?", (event_id,))
        return rows[0] if rows else None

    def require_consumption(self, event_id: int) -> dict:
        ev = self.get_consumption(event_id)
        if ev is None:
            raise LogbookError(f"Kulua {event_id} ei ole tämän auton ({self.vehicle_id}) kirjanpidossa.")
        return ev

    def list_consumption(
        self, start_iso: str | None = None, end_iso: str | None = None, include_voided: bool = False, kind: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM consumption_events WHERE 1=1"
        params: list[Any] = []
        if not include_voided:
            sql += " AND voided=0"
        if start_iso:
            sql += " AND start_time >= ?"
            params.append(start_iso)
        if end_iso:
            sql += " AND start_time < ?"
            params.append(end_iso)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        return self.query(sql + " ORDER BY start_time, id", params)

    def update_consumption(
        self, event_id: int, field: str, value: Any, source: str = "ui", reason: str | None = None
    ) -> dict:
        """Yksikköhinta ja kommentti ovat vapaita (myös lukitulla jaksolla). Määrän korjaus ja
        mitätöinti vaativat perustelun eivätkä onnistu lukitulla jaksolla."""
        if field not in ("unit_price", "comment", "quantity", "voided"):
            raise LogbookError(f"Kenttää {field} ei voi muokata.")
        if source not in CHANGE_SOURCES:
            raise ValueError(source)
        ev = self.require_consumption(event_id)
        y, m = timeutil.local_year_month(ev["start_time"])
        locked = self.is_locked(y, m)
        if field in ("quantity", "voided"):
            if locked:
                raise PeriodLockedError(f"Jakso {m:02d}/{y} on lukittu. Määrää ei voi muuttaa.")
            if not (reason or "").strip():
                raise LogbookError("Mitatun määrän korjaus tai kulun poisto vaatii perustelun.")
        if field == "unit_price":
            if value in (None, ""):
                value = None
            else:
                try:
                    value = float(str(value).replace(",", ".").replace("€", "").strip())
                except ValueError as exc:
                    raise LogbookError(f"Yksikköhinta '{value}' ei ole luku.") from exc
                if value < 0:
                    raise LogbookError("Yksikköhinta ei voi olla negatiivinen.")
                value = round(value, 4)
        elif field == "quantity":
            try:
                value = round(float(str(value).replace(",", ".")), 2)
            except ValueError as exc:
                raise LogbookError(f"Määrä '{value}' ei ole luku.") from exc
            if value <= 0:
                raise LogbookError("Määrän on oltava suurempi kuin nolla.")
        elif field == "voided":
            value = 1 if str(value).lower() in ("1", "true", "on", "yes") else 0
        else:
            value = "" if value is None else str(value).strip()
        old = ev[field]
        if _s(old) == _s(value) or (old in (None, "") and value in (None, "")):
            return ev
        sets = {field: value, "updated_at": timeutil.now_iso()}
        if field == "quantity":
            sets["corrected"] = 1
        with self.tx() as c:
            c.execute(
                f"UPDATE consumption_events SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                [*sets.values(), event_id],
            )
            if locked:
                reason = (reason + "; " if reason else "") + "muutettu lukituksen jälkeen"
            self._log(c, None, f"consumption:{event_id}:{field}", old, value, source, reason)
        return self.require_consumption(event_id)

    def set_consumption_address(self, event_id: int, address: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE consumption_events SET address=? WHERE id=? AND address=''", (address, event_id))

    def add_fuel_reading(self, ts_iso: str, litres: float) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO fuel_readings(ts, litres) VALUES (?, ?)", (ts_iso, float(litres)))

    def fuel_readings(self, start_iso: str | None = None, end_iso: str | None = None) -> list[dict]:
        sql, params = "SELECT ts, litres FROM fuel_readings WHERE 1=1", []
        if start_iso:
            sql += " AND ts >= ?"
            params.append(start_iso)
        if end_iso:
            sql += " AND ts < ?"
            params.append(end_iso)
        return self.query(sql + " ORDER BY ts", params)

    def fuel_reading_before(self, iso: str) -> dict | None:
        rows = self.query("SELECT ts, litres FROM fuel_readings WHERE ts < ? ORDER BY ts DESC LIMIT 1", (iso,))
        return rows[0] if rows else None

    # ------------------------------------------------------- ylläpito ja varmuus
    def integrity_check(self) -> str:
        with self._lock:
            return self.conn.execute("PRAGMA integrity_check").fetchone()[0]

    def backup_to(self, dest: str | Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()
