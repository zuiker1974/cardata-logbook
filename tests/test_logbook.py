import io
import sqlite3
from datetime import datetime, timezone

import pytest
from helpers import settings
from openpyxl import load_workbook

from logbook import timeutil
from logbook.database import LogbookError, PeriodLockedError, VehicleDB
from logbook.integrity import odometer_issues
from logbook.service import LockBlocked, VehicleService
from logbook.xlsx_report import ImportRejected


def make_service(tmp_path, vehicle_id="530e"):
    s = settings(export_dir=str(tmp_path / "share"))
    s.data_dir = str(tmp_path / "data")
    return VehicleService(s.vehicle(vehicle_id), s)


def trip(day, hour, odo_start, km, ttype="private", purpose="", month=10):
    start = datetime(2026, month, day, hour, 0, tzinfo=timezone.utc)
    end = start.replace(minute=40)
    return {
        "start_time": timeutil.to_iso(start),
        "end_time": timeutil.to_iso(end),
        "start_lat": 61.49,
        "start_lon": 23.76,
        "end_lat": 61.45,
        "end_lon": 23.85,
        "start_address": "Kotikatu 1, Tampere",
        "end_address": "Työkatu 5, Tampere",
        "start_odometer": odo_start,
        "end_odometer": odo_start + km,
        "trip_type": ttype,
        "purpose": purpose,
    }


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def svc(tmp_path):
    svc = make_service(tmp_path)
    svc.db.insert_trip(trip(1, 6, 10000, 15, "business", "Asiakas Oy, huolto"))
    svc.db.insert_trip(trip(1, 15, 10015, 15, "commute"))
    svc.db.insert_trip(trip(2, 8, 10030, 40))
    return svc


def test_database_belongs_to_one_vehicle(tmp_path):
    path = tmp_path / "x.db"
    VehicleDB(path, "530e").close()
    with pytest.raises(LogbookError):
        VehicleDB(path, "ix3")
    db = VehicleDB(tmp_path / "y.db", "530e")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO trips(vehicle_id, start_time, end_time, created_at) VALUES ('ix3', 'a', 'b', 'c')"
        )


def test_change_log_is_append_only(svc):
    svc.db.update_field(1, "comment", "Hyvä keli", source="ui")
    with pytest.raises(sqlite3.IntegrityError):
        svc.db.conn.execute("UPDATE trip_changes SET new_value='x'")
    with pytest.raises(sqlite3.IntegrityError):
        svc.db.conn.execute("DELETE FROM trip_changes")


def test_correction_needs_reason_and_keeps_original(svc):
    with pytest.raises(LogbookError):
        svc.db.update_field(3, "end_odometer", 10072)
    t = svc.db.update_field(3, "end_odometer", 10072, reason="Mittari näytti 10072")
    assert t["distance_km"] == 42 and t["corrected"] == 1
    assert t["orig_end_odometer"] == 10070
    hist = svc.db.history(3)
    assert hist[-1]["reason"] == "Mittari näytti 10072"


def test_merge_only_within_same_vehicle(tmp_path, svc):
    other = make_service(tmp_path, "ix3")
    other_id = other.db.insert_trip(trip(3, 8, 500, 10))
    # id 99 ei ole 530e:n tietokannassa, vaikka iX3:lla olisi samanlainen id
    with pytest.raises(LogbookError):
        svc.db.merge([1, 99])
    assert other_id == 1  # sama numero, eri auto: yhdistäminen käyttää vain oman auton tietokantaa
    new_id = svc.db.merge([1, 2])
    merged = svc.db.get_trip(new_id)
    assert merged["distance_km"] == 30 and merged["trip_type"] == "business"
    assert [t["id"] for t in svc.db.list_trips()] == [new_id, 3]
    svc.db.unmerge(new_id)
    assert [t["id"] for t in svc.db.list_trips()] == [1, 2, 3]


def test_gap_detection_fill_and_ack(svc):
    svc.db.insert_trip(trip(3, 8, 10100, 10))  # aukko 10070 -> 10100
    issues = [i for i in odometer_issues(svc.db, 1.0) if i.kind == "gap"]
    assert len(issues) == 1 and issues[0].km == 30 and issues[0].blocking
    svc.fill_gap(issues[0].prev_trip_id, issues[0].next_trip_id)
    assert [i for i in odometer_issues(svc.db, 1.0) if i.kind == "gap"] == []
    svc.db.insert_trip(trip(4, 8, 10200, 10))
    gap = [i for i in odometer_issues(svc.db, 1.0) if i.kind == "gap"][0]
    svc.db.ack_gap(gap.prev_trip_id, gap.next_trip_id, "Huollon koeajo")
    gap = [i for i in odometer_issues(svc.db, 1.0) if i.kind == "gap"][0]
    assert not gap.blocking and gap.ack_reason == "Huollon koeajo"


def test_lock_requires_checks_and_archives_with_hash(svc):
    svc.db.update_field(1, "purpose", "")
    with pytest.raises(LockBlocked):
        svc.lock_period(2026, 10, now=NOW)
    svc.db.update_field(1, "purpose", "Asiakas Oy, huolto")
    with pytest.raises(LogbookError):
        svc.lock_period(2026, 11, now=NOW)  # kuukausi ei ole päättynyt
    p = svc.lock_period(2026, 10, now=NOW)
    assert p["status"] == "locked" and len(p["report_sha256"]) == 64
    archive = svc.exporter.archive_dir(2026)
    names = sorted(x.name for x in archive.iterdir())
    assert "Ajopaivakirja_530e_ABC-123_2026_10.pdf" in names
    assert "Ajopaivakirja_530e_ABC-123_2026_10_tyoajot.pdf" in names
    assert "Ajopaivakirja_530e_ABC-123_2026_10.pdf.sha256" in names
    with pytest.raises(PeriodLockedError):
        svc.db.update_field(1, "purpose", "muutos")
    with pytest.raises(PeriodLockedError):
        svc.db.merge([2, 3])
    # jälkikommentti sallitaan ja merkitään
    svc.db.update_field(1, "comment", "Jälkikommentti")
    assert "lukituksen jälkeen" in svc.db.history(1)[-1]["reason"]
    # avaus vaatii perustelun, uudelleenlukitus tuottaa uuden version vanhaa poistamatta
    with pytest.raises(LogbookError):
        svc.unlock_period(2026, 10, "")
    svc.unlock_period(2026, 10, "Tarkoitus tarkennettu")
    svc.db.update_field(1, "purpose", "Asiakas Oy, vuosihuolto")
    svc.lock_period(2026, 10, now=NOW)
    names = sorted(x.name for x in archive.iterdir())
    assert "Ajopaivakirja_530e_ABC-123_2026_10_v2.pdf" in names
    assert "Ajopaivakirja_530e_ABC-123_2026_10.pdf" in names


def test_reports_are_written_per_vehicle(svc):
    svc.mark_dirty_time(svc.db.get_trip(1)["start_time"])
    assert svc.write_reports_if_due(force=True)
    car = svc.exporter.car_dir
    assert car.name == "530e_ABC-123"
    files = {p.name for p in car.rglob("*") if p.is_file()}
    assert "Ajopaivakirja_530e_ABC-123_2026_10.pdf" in files
    assert "Ajopaivakirja_530e_ABC-123_2026_10_yksityisajot.pdf" in files
    assert "Ajopaivakirja_530e_ABC-123_2026_vuosiraportti.pdf" in files
    assert "Ajopaivakirja_530e_ABC-123_2026.xlsx" in files
    assert not any(n.endswith(".tmp") for n in files)
    assert (car / "10" if False else car / "2026" / "10").is_dir()
    backup = svc.backup_db(check=True)
    assert backup.name.startswith("bmw_logbook_530e_backup_")


def test_excel_roundtrip_comments_and_conflicts(svc):
    data = svc.ctx and __import__("logbook.xlsx_report", fromlist=["build_xlsx"]).build_xlsx(svc.ctx, 2026)
    wb = load_workbook(io.BytesIO(data))
    assert {"Yhteenveto", "10", "Työajot", "Yksityisajot", "Työmatkat", "_meta"} <= set(wb.sheetnames)
    ws = wb["10"]
    assert ws.column_dimensions["M"].hidden
    rows = {ws.cell(row=r, column=13).value: r for r in range(4, ws.max_row + 1)}
    ws.cell(row=rows[1], column=12, value="Kommentti Excelistä")
    ws.cell(row=rows[3], column=9, value="Työajo")
    ws.cell(row=rows[3], column=10, value="Työmaakäynti")
    ws.cell(row=rows[2], column=7, value=99999)  # lukittu sarake
    ws.cell(row=rows[2], column=12, value="Excel-kommentti 2")
    # sama matka muutettu myös selaimessa viennin jälkeen
    svc.db.update_field(2, "comment", "Selainkommentti")
    buf = io.BytesIO()
    wb.save(buf)
    res = svc.import_bytes(buf.getvalue(), "testi.xlsx")
    t1, t2, t3 = (svc.db.get_trip(i) for i in (1, 2, 3))
    assert t1["comment"] == "Kommentti Excelistä" and t1["comment_source"] == "excel"
    assert t3["trip_type"] == "business" and t3["purpose"] == "Työmaakäynti"
    assert t2["comment"] == "Selainkommentti | Excel-kommentti 2"
    assert t2["end_odometer"] == 10030
    assert res.locked_changes and res.conflicts
    assert svc.db.history(1)[-1]["source"] == "excel_upload"
    assert svc.db.imports()[0]["changed_rows"] == 3


def test_excel_purpose_conflict_requires_choice(svc):
    from logbook.xlsx_report import build_xlsx

    wb = load_workbook(io.BytesIO(build_xlsx(svc.ctx, 2026)))
    ws = wb["10"]
    ws.cell(row=4, column=10, value="Excel-tarkoitus")
    svc.db.update_field(1, "purpose", "Selain-tarkoitus")
    buf = io.BytesIO()
    wb.save(buf)
    svc.import_bytes(buf.getvalue(), "t.xlsx")
    assert svc.db.get_trip(1)["purpose"] == "Selain-tarkoitus"
    conflicts = svc.db.open_conflicts()
    assert len(conflicts) == 1
    svc.db.resolve_conflict(conflicts[0]["id"], "excel")
    assert svc.db.get_trip(1)["purpose"] == "Excel-tarkoitus"


def test_other_vehicles_excel_is_rejected(tmp_path, svc):
    from logbook.xlsx_report import build_xlsx

    other = make_service(tmp_path, "ix3")
    other.db.insert_trip(trip(3, 8, 500, 10))
    data = build_xlsx(other.ctx, 2026)
    with pytest.raises(ImportRejected):
        svc.import_bytes(data, "ix3.xlsx")
    # Driven kautta: hylätty tiedosto siirretään hylatyt-kansioon
    tuonti = svc.exporter.car_dir / "tuonti"
    (tuonti / "ix3.xlsx").write_bytes(data)
    import os
    import time

    old = time.time() - 60
    os.utime(tuonti / "ix3.xlsx", (old, old))
    results = svc.process_import_folder()
    assert "hylätty" in results[0][1]
    assert list((svc.exporter.car_dir / "tuonnit" / "hylatyt").iterdir())


def test_comment_to_merged_trip_moves_to_parent(svc):
    from logbook.xlsx_report import build_xlsx

    wb = load_workbook(io.BytesIO(build_xlsx(svc.ctx, 2026)))
    parent = svc.db.merge([1, 2])
    wb["10"].cell(row=5, column=12, value="Kommentti osamatkalle")
    buf = io.BytesIO()
    wb.save(buf)
    svc.import_bytes(buf.getvalue(), "m.xlsx")
    assert svc.db.get_trip(parent)["comment"] == "Kommentti osamatkalle"


def test_filtered_reports(svc):
    from logbook.pdf_report import build_month_pdf, build_year_pdf
    from logbook.report_data import ReportFilter
    from logbook.xlsx_report import build_xlsx

    flt = ReportFilter.parse("private", True, svc.settings)
    assert flt.types == ("private", "commute")
    assert svc.ctx.month_pdf_name(2026, 10, flt) == "Ajopaivakirja_530e_ABC-123_2026_10_yksityisajot.pdf"
    assert build_month_pdf(svc.ctx, 2026, 10, flt)[:4] == b"%PDF"
    assert build_year_pdf(svc.ctx, 2026, ReportFilter.parse("business", None, svc.settings))[:4] == b"%PDF"
    wb = load_workbook(io.BytesIO(build_xlsx(svc.ctx, 2026, ReportFilter.parse("business", None, svc.settings))))
    ws = wb["10"]
    ids = [ws.cell(row=r, column=13).value for r in range(4, ws.max_row + 1) if ws.cell(row=r, column=13).value]
    assert ids == [1]
    assert "Työajot" not in wb.sheetnames
    assert wb["_meta"]["B8"].value == "business"
