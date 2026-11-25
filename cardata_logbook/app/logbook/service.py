"""Yhden auton palvelu: tietokanta, raporttien kirjoitus, lukitus ja tuonnit yhdessä.

Jokaisella autolla on oma VehicleService-olio. Mikään metodi ei käsittele toisen auton dataa.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import timeutil
from .csv_report import build_csv
from .database import LogbookError, VehicleDB
from .exporter import Exporter, atomic_write
from .integrity import content_hash, file_sha256, odometer_issues, period_issues
from .pdf_report import build_month_pdf, build_year_pdf
from .report_data import ReportContext, ReportFilter
from .vehicle_registry import Settings, VehicleConfig
from .xlsx_report import ImportRejected, ImportResult, build_xlsx, import_xlsx

_LOG = logging.getLogger(__name__)

EXPORT_DELAY_S = 300  # raportit kirjoitetaan 5 min viiveellä muutoksen jälkeen


class LockBlocked(LogbookError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("Jaksoa ei voi lukita: " + "; ".join(i.message for i in issues))


class VehicleService:
    def __init__(self, vehicle: VehicleConfig, settings: Settings, db: VehicleDB | None = None):
        self.vehicle = vehicle
        self.settings = settings
        data_dir = Path(settings.data_dir) / vehicle.id
        self.db = db or VehicleDB(data_dir / "logbook.db", vehicle.id)
        self.db.sync_vehicle_info(vehicle)
        self.ctx = ReportContext(self.db, vehicle, settings)
        self.exporter = Exporter(settings, vehicle)
        self.state_path = data_dir / "active_trip.json"
        self.consumption_state_path = data_dir / "consumption_state.json"
        self.trip_manager = None  # asetetaan main.py:ssä
        self.consumption = None  # asetetaan main.py:ssä
        self.dirty_months: set[tuple[int, int]] = set()
        self.dirty_since: datetime | None = None
        self.last_export: str | None = self.db.get_state("last_export")
        self.last_import: str | None = self.db.get_state("last_import")
        self.entity_status: dict[str, dict] = {}
        self.warnings: list[str] = []
        self.integrity_ok: bool = True
        self._lock = threading.RLock()
        try:
            self.exporter.ensure_dirs()
        except OSError as exc:
            self.warnings.append(f"Raporttikansiota ei voitu luoda: {exc}")

    @property
    def vehicle_id(self) -> str:
        return self.vehicle.id

    # ------------------------------------------------------------- matkat
    def on_trip_finished(self, trip: dict) -> int:
        trip_id = self.db.insert_trip(trip, source="auto")
        _LOG.info("%s: tallennettu matka %s (%.1f km)", self.vehicle_id, trip_id, trip.get("distance_km") or 0)
        self.mark_dirty_time(trip["start_time"])
        try:
            self.backup_db()
        except Exception:
            _LOG.exception("%s: varmuuskopio epäonnistui", self.vehicle_id)
        return trip_id

    # ---------------------------------------------------------------- kulut
    def on_consumption(self, event: dict) -> int:
        eid = self.db.insert_consumption(event)
        _LOG.info(
            "%s: tallennettu kulu %s (%s %.2f %s)", self.vehicle_id, eid, event["kind"], event["quantity"], event.get("unit", "")
        )
        self.mark_dirty_time(event["start_time"])
        return eid

    def on_fuel_reading(self, ts_iso: str, litres: float) -> None:
        self.db.add_fuel_reading(ts_iso, litres)
        self.mark_dirty_time(ts_iso)

    def add_manual_consumption(self, event: dict, reason: str = "") -> int:
        event = dict(event, source="manual")
        eid = self.db.insert_consumption(event, reason=reason)
        self.mark_dirty_time(event["start_time"])
        return eid

    def update_consumption(self, event_id: int, field: str, value, source: str = "ui", reason: str | None = None) -> dict:
        ev = self.db.update_consumption(event_id, field, value, source=source, reason=reason)
        self.mark_dirty_time(ev["start_time"])
        return ev

    def mark_dirty_time(self, iso: str) -> None:
        with self._lock:
            self.dirty_months.add(timeutil.local_year_month(iso))
            if self.dirty_since is None:
                self.dirty_since = timeutil.now_utc()

    def mark_dirty_trip(self, trip_id: int) -> None:
        trip = self.db.get_trip(trip_id)
        if trip:
            self.mark_dirty_time(trip["start_time"])

    # ------------------------------------------------------------ raportit
    def write_reports_if_due(self, now: datetime | None = None, force: bool = False) -> bool:
        now = now or timeutil.now_utc()
        with self._lock:
            if not self.dirty_months:
                return False
            if not force and self.dirty_since and (now - self.dirty_since).total_seconds() < EXPORT_DELAY_S:
                return False
            months = sorted(self.dirty_months)
            self.dirty_months.clear()
            self.dirty_since = None
        for year, month in months:
            self.write_month(year, month)
        for year in sorted({y for y, _ in months}):
            self.write_year(year)
        self.last_export = timeutil.now_iso()
        self.db.set_state("last_export", self.last_export)
        return True

    def write_month(self, year: int, month: int) -> list[Path]:
        written = []
        folder = self.exporter.month_dir(year, month)
        for key in ("all", "business", "private"):
            flt = ReportFilter.parse(key, None, self.settings)
            data = build_month_pdf(self.ctx, year, month, flt)
            written.append(self.exporter.write(folder / self.ctx.month_pdf_name(year, month, flt), data))
        return written

    def write_year(self, year: int) -> list[Path]:
        written = []
        folder = self.exporter.year_dir(year)
        written.append(self.exporter.write(folder / self.ctx.year_pdf_name(year), build_year_pdf(self.ctx, year)))
        if self.settings.report_xlsx:
            written.append(self.exporter.write(folder / self.ctx.xlsx_name(year), build_xlsx(self.ctx, year)))
        if self.settings.report_csv:
            written.append(self.exporter.write(folder / self.ctx.csv_name(year), build_csv(self.ctx, year)))
        return written

    def backup_db(self, now: datetime | None = None, check: bool = False) -> Path | None:
        now = (now or timeutil.now_utc()).astimezone(timeutil.local_tz())
        if check:
            result = self.db.integrity_check()
            self.integrity_ok = result == "ok"
            if not self.integrity_ok:
                msg = f"Tietokannan eheystarkistus epäonnistui ({result}). Varmuuskopiota ei korvattu."
                _LOG.error("%s: %s", self.vehicle_id, msg)
                if msg not in self.warnings:
                    self.warnings.append(msg)
                return None
        target = self.exporter.backup_path(now)
        tmp = target.with_name(target.name + ".tmp")
        self.db.backup_to(tmp)
        tmp.replace(target)
        self.exporter.cleanup_backups(now)
        return target

    # ---------------------------------------------------------- lukitus
    def lock_period(self, year: int, month: int, now: datetime | None = None, auto: bool = False) -> dict:
        now = now or timeutil.now_utc()
        if self.db.is_locked(year, month):
            raise LogbookError(f"Jakso {month:02d}/{year} on jo lukittu.")
        _, end = timeutil.month_bounds(year, month)
        if timeutil.parse_iso(end) > now:
            raise LogbookError("Kuukautta ei voi lukita ennen kuin se on päättynyt.")
        self.process_import_folder()
        issues = [i for i in period_issues(self.db, year, month, self.settings.odometer_gap_tolerance_km) if i.blocking]
        if issues:
            raise LockBlocked(issues)
        start, end = timeutil.month_bounds(year, month)
        trips = self.db.list_trips(start, end)
        chash = content_hash(self.vehicle_id, year, month, trips)
        version = self.db.period(year, month).get("version", 0) + 1
        note = f"Lukittu arkistoversio {version} · Sisällön SHA-256: {chash}"
        data = build_month_pdf(self.ctx, year, month, ReportFilter(), footer_note=note)
        path = self.exporter.write_archive(self.ctx.month_pdf_name(year, month), data, year)
        fhash = file_sha256(path)
        atomic_write(path.with_name(path.name + ".sha256"), f"{fhash}  {path.name}\n".encode())
        for key in self.settings.archive_filtered or []:
            flt = ReportFilter.parse(key, None, self.settings)
            fdata = build_month_pdf(self.ctx, year, month, flt, footer_note=note)
            fpath = self.exporter.write_archive(self.ctx.month_pdf_name(year, month, flt), fdata, year)
            atomic_write(fpath.with_name(fpath.name + ".sha256"), f"{file_sha256(fpath)}  {fpath.name}\n".encode())
        s = self.ctx.month_summary(year, month)
        self.db.set_period_locked(
            year,
            month,
            report_sha256=fhash,
            content_sha256=chash,
            archive_path=str(path),
            version=version,
            odometer_start=s.get("odometer_start"),
            odometer_end=s.get("odometer_end"),
            reason="Automaattinen lukitus" if auto else "Lukittu käyttöliittymästä",
        )
        _LOG.info("%s: jakso %02d/%s lukittu, SHA-256 %s", self.vehicle_id, month, year, fhash)
        self.mark_dirty_time(start)
        self._archive_year_if_complete(year)
        return self.db.period(year, month)

    def _archive_year_if_complete(self, year: int) -> None:
        start, end = timeutil.year_bounds(year)
        trips = self.db.list_trips(start, end)
        if not trips:
            return
        first_month = timeutil.local_year_month(trips[0]["start_time"])[1]
        if not all(self.db.is_locked(year, m) for m in range(first_month, 13)):
            return
        self.exporter.write_archive(self.ctx.year_pdf_name(year), build_year_pdf(self.ctx, year), year)
        self.exporter.write_archive(self.ctx.xlsx_name(year), build_xlsx(self.ctx, year), year)
        _LOG.info("%s: vuosi %s arkistoitu", self.vehicle_id, year)

    def unlock_period(self, year: int, month: int, reason: str) -> None:
        self.db.unlock_period(year, month, reason)
        self.mark_dirty_time(timeutil.month_bounds(year, month)[0])

    def auto_lock(self, now: datetime | None = None) -> list[tuple[int, int]]:
        days = self.settings.auto_lock_after_days
        if not days:
            return []
        now = now or timeutil.now_utc()
        first = self.db.list_trips()
        if not first:
            return []
        y, m = timeutil.local_year_month(first[0]["start_time"])
        locked = []
        while True:
            _, end = timeutil.month_bounds(y, m)
            if timeutil.parse_iso(end) + timedelta(days=days) > now:
                break
            if not self.db.is_locked(y, m):
                try:
                    self.lock_period(y, m, now=now, auto=True)
                    locked.append((y, m))
                except LogbookError as exc:
                    _LOG.info("%s: jaksoa %02d/%s ei lukittu automaattisesti: %s", self.vehicle_id, m, y, exc)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return locked

    def period_file(self, year: int, month: int) -> Path | None:
        p = self.db.period(year, month)
        if p.get("status") == "locked" and p.get("archive_path") and Path(p["archive_path"]).exists():
            return Path(p["archive_path"])
        return None

    # ------------------------------------------------------- aukot ja lisäys
    def fill_gap(self, prev_id: int, next_id: int, trip_type: str = "private", purpose: str = "", reason: str = "") -> int:
        prev = self.db.require_trip(prev_id)
        nxt = self.db.require_trip(next_id)
        trip = {
            "start_time": prev["end_time"],
            "end_time": nxt["start_time"],
            "start_lat": prev["end_lat"],
            "start_lon": prev["end_lon"],
            "start_address": prev["end_address"],
            "end_lat": nxt["start_lat"],
            "end_lon": nxt["start_lon"],
            "end_address": nxt["start_address"],
            "start_odometer": prev["end_odometer"],
            "end_odometer": nxt["start_odometer"],
            "trip_type": trip_type or "private",
            "purpose": purpose,
        }
        tid = self.db.insert_trip(trip, source="gap_fill", reason=reason or "Mittariaukosta luotu ajo")
        self.mark_dirty_time(trip["start_time"])
        return tid

    def add_manual_trip(self, trip: dict, reason: str = "") -> int:
        tid = self.db.insert_trip(trip, source="manual", reason=reason or "Käsin lisätty ajo")
        self.mark_dirty_time(trip["start_time"])
        return tid

    def issues(self, start: str | None = None, end: str | None = None):
        return odometer_issues(self.db, self.settings.odometer_gap_tolerance_km, start, end)

    # ---------------------------------------------------------------- tuonnit
    def import_bytes(self, data: bytes, filename: str, source: str = "excel_upload") -> ImportResult:
        try:
            res = import_xlsx(self.db, self.vehicle_id, self.vehicle.vin, data, filename, source=source)
        except ImportRejected as exc:
            self.db.log_import(source, filename, "rejected", 0, {"error": str(exc)})
            self.last_import = f"{timeutil.fmt_datetime(timeutil.now_iso())}: hylätty ({exc})"
            self.db.set_state("last_import", self.last_import)
            raise
        for tid in res.changed_trip_ids:
            self.mark_dirty_trip(tid)
        for eid in res.changed_consumption_ids:
            ev = self.db.get_consumption(eid)
            if ev:
                self.mark_dirty_time(ev["start_time"])
        self.last_import = f"{timeutil.fmt_datetime(timeutil.now_iso())}: {res.status_text()}"
        self.db.set_state("last_import", self.last_import)
        return res

    def process_import_folder(self) -> list[tuple[str, str]]:
        results = []
        for path in self.exporter.pending_imports():
            try:
                res = self.import_bytes(path.read_bytes(), path.name, source="excel_drive")
                self.exporter.move_processed(path)
                results.append((path.name, res.status_text()))
            except ImportRejected as exc:
                self.exporter.move_processed(path, rejected=True)
                results.append((path.name, f"hylätty: {exc}"))
            except Exception as exc:  # pragma: no cover - odottamaton virhe, tiedosto jää paikalleen
                _LOG.exception("%s: tuonti %s epäonnistui", self.vehicle_id, path)
                results.append((path.name, f"virhe: {exc}"))
        return results

    # ------------------------------------------------------------ apu
    def record_odometer_boundary(self, value: float, ts_iso: str, previous: float | None) -> None:
        y, m = timeutil.local_year_month(ts_iso)
        key = f"odometer_boundary:{y}-{m:02d}"
        if self.db.get_state(key) is None:
            self.db.set_state(key, str(previous if previous is not None else value))
