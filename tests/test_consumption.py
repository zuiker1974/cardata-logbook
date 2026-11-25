"""Kululaskenta: lataukset (kotilaturi ja arvio), tankkaukset ja raportit, aina autokohtaisesti."""
from datetime import timedelta

import io

from helpers import HOME, T0, settings
from openpyxl import load_workbook
from test_logbook import make_service, trip

from logbook import timeutil
from logbook.consumption import ConsumptionTracker
from logbook.pdf_report import build_month_pdf, build_year_pdf
from logbook.report_data import consumption_summary
from logbook.trip_manager import Zone
from logbook.xlsx_report import build_xlsx

ABB = "sensor.home_charger_energy"


def cfg(**over):
    opts = {}
    s = settings(**over)
    v = s.vehicle("530e")
    v.entities["fuel_level"] = "sensor.530e_xdrive_range_tank_level"
    v.entities["charger_energy"] = ABB
    return s, v, opts


class CSim:
    def __init__(self, s=None, v=None, home=True):
        if s is None:
            s, v, _ = cfg()
        self.events = []
        self.readings = []
        self.ct = ConsumptionTracker(
            v,
            s,
            on_charge=self.events.append,
            on_refuel=self.events.append,
            on_fuel_reading=lambda ts, l: self.readings.append((ts, l)),
            zones=[Zone("zone.home", HOME[0], HOME[1], 100)],
        )
        self.t = T0
        if home:
            self.at(0).ev("location", "home", {"latitude": HOME[0], "longitude": HOME[1]})

    def at(self, minutes):
        self.t = T0 + timedelta(minutes=minutes)
        return self

    def ev(self, role, state, attrs=None):
        self.ct.process(role, str(state), attrs or {}, self.t)
        return self

    def wh(self, value):
        return self.ev("charger_energy", value, {"unit_of_measurement": "Wh"})


def test_home_charge_uses_wallbox_counter_even_when_car_reports_plug_late():
    # 21.11.2026: laturi alkoi laskea 19.20, auto ilmoitti kytkennän vasta 19.26
    sim = CSim()
    sim.at(1).ev("soc", 2).wh(0)
    sim.at(10).wh(15)
    sim.at(16).wh(330).ev("charging_method", "AC_TYPE2PLUG")
    for i, v in enumerate(range(1000, 11001, 1000)):
        sim.at(20 + i * 15).wh(v)
    sim.at(200).ev("soc", 100)
    sim.at(210).wh(11417)
    sim.at(300).wh(0)  # kaapeli irti laturista: laskuri nollautuu ennen auton tietoa
    sim.at(340).ev("charging_method", "NOCHARGING")
    assert len(sim.events) == 1
    e = sim.events[0]
    assert e["kind"] == "charge" and e["source"] == "charger"
    assert e["quantity"] == 11.42
    assert e["soc_start"] == 2 and e["soc_end"] == 100
    assert e["at_home"] == 1
    assert e["estimated_kwh"] == 11.76  # 98 % x 0,12 kWh/%


def test_counter_reset_mid_session_adds_parts():
    sim = CSim()
    sim.at(1).ev("soc", 20).wh(0).ev("charging_method", "AC_TYPE2PLUG")
    sim.at(30).wh(3000)
    sim.at(31).wh(0)  # laturin istunto katkesi ja alkoi uudelleen
    sim.at(60).wh(2500)
    sim.at(90).ev("charging_method", "NOCHARGING")
    assert sim.events[0]["quantity"] == 5.5


def test_old_counter_value_from_previous_session_is_not_counted():
    sim = CSim()
    sim.at(1).wh(7000)  # edellinen istunto jäi laskuriin (ei nollautunut)
    sim.at(300).ev("soc", 50).ev("charging_method", "AC_TYPE2PLUG")
    sim.at(330).wh(8000)
    sim.at(400).ev("charging_method", "NOCHARGING")
    assert sim.events[0]["quantity"] == 1.0


def test_away_charge_is_estimated_from_soc():
    sim = CSim(home=False)
    sim.at(0).ev("location", "not_home", {"latitude": 62.24, "longitude": 25.72})
    sim.at(1).ev("soc", 10).ev("charging_method", "AC_TYPE2PLUG")
    sim.at(2).wh(5000)  # kotilaturi lataa toista autoa: ei kuulu tälle
    sim.at(100).ev("soc", 60)
    sim.at(120).ev("charging_method", "NOCHARGING")
    e = sim.events[0]
    assert e["source"] == "estimate" and e["quantity"] == 6.0 and e["at_home"] == 0


def test_plug_without_energy_is_not_logged_and_drive_away_closes_session():
    sim = CSim()
    sim.at(1).ev("soc", 100).wh(0).ev("charging_method", "AC_TYPE2PLUG")
    sim.at(30).ev("location", "not_home", {"latitude": HOME[0] + 0.02, "longitude": HOME[1]})
    assert sim.events == [] and sim.ct.s.plug is None


def test_refuel_detection_and_noise():
    sim = CSim()
    for minute, litres in ((0, 39), (60, 34), (120, 29), (180, 28), (240, 10)):
        sim.at(minute).ev("fuel_level", litres)
    sim.at(600).ev("fuel_level", 40)
    sim.at(610).ev("fuel_level", 43)  # nousu jatkuu: sama tankkaus
    sim.at(700).ev("fuel_level", 42)
    sim.at(800).ev("fuel_level", 44)  # pieni heilahdus ei ole tankkaus
    sim.at(900).ev("fuel_level", 43)
    assert [(e["kind"], e["quantity"], e["fuel_before"], e["fuel_after"]) for e in sim.events] == [("refuel", 33, 10, 43)]
    assert len(sim.readings) == 10


def test_pending_refuel_finishes_on_tick():
    sim = CSim()
    sim.at(0).ev("fuel_level", 12)
    sim.at(10).ev("fuel_level", 45)
    assert sim.events == []
    sim.ct.tick(T0 + timedelta(minutes=80))
    assert sim.events[0]["quantity"] == 33


def test_bev_has_no_fuel():
    s = settings()
    v = s.vehicle("ix3")
    v.entities["fuel_level"] = "sensor.x"
    sim = CSim(s, v)
    sim.at(0).ev("fuel_level", 10).at(5).ev("fuel_level", 40)
    sim.ct.tick(T0 + timedelta(hours=3))
    assert sim.events == [] and sim.readings == []


def test_state_survives_restart(tmp_path):
    s, v, _ = cfg()
    path = tmp_path / "c.json"
    out = []
    ct = ConsumptionTracker(v, s, on_charge=out.append, state_path=path, zones=[Zone("zone.home", HOME[0], HOME[1], 100)])
    ct.process("location", "home", {"latitude": HOME[0], "longitude": HOME[1]}, T0)
    ct.process("charger_energy", "0", {"unit_of_measurement": "Wh"}, T0)
    ct.process("charging_method", "AC_TYPE2PLUG", {}, T0 + timedelta(minutes=1))
    ct.process("charger_energy", "4000", {"unit_of_measurement": "Wh"}, T0 + timedelta(minutes=60))
    ct2 = ConsumptionTracker(v, s, on_charge=out.append, state_path=path, zones=[Zone("zone.home", HOME[0], HOME[1], 100)])
    ct2.process("charging_method", "NOCHARGING", {}, T0 + timedelta(minutes=90))
    assert out[0]["quantity"] == 4.0


# --------------------------------------------------------------- tallennus ja raportit
def _ev(month, day, hour, kind="charge", qty=10.0, source="charger", **kw):
    start = timeutil.local_naive_to_iso(f"2026-{month:02d}-{day:02d}T{hour:02d}:00")
    base = {"kind": kind, "start_time": start, "end_time": start, "quantity": qty, "source": source, "at_home": 1}
    base.update(kw)
    return base


def test_reports_contain_consumption_per_vehicle(tmp_path):
    a = make_service(tmp_path, "530e")
    b = make_service(tmp_path, "ix3")
    a.db.insert_trip(trip(5, 8, 10000, 100))
    a.on_consumption(_ev(10, 5, 20, soc_start=5, soc_end=100))
    a.on_consumption(_ev(10, 6, 20, qty=6.0, source="estimate", address="Jyväskylä"))
    a.on_consumption(_ev(10, 7, 12, kind="refuel", qty=33, source="tank_level", fuel_before=10, fuel_after=43))
    for day, litres in ((1, 20), (6, 10), (7, 43), (20, 38)):
        a.on_fuel_reading(timeutil.local_naive_to_iso(f"2026-10-{day:02d}T12:00"), litres)
    b.on_consumption(_ev(10, 5, 21, qty=55))
    ms, me = timeutil.month_bounds(2026, 10)
    s = consumption_summary(a.db, a.vehicle, ms, me, 100)
    assert s["charged_kwh"] == 16.0 and s["charged_estimated_kwh"] == 6.0
    assert s["refuelled_l"] == 33 and s["fuel_used_l"] == 15 and s["fuel_per_100km"] == 15.0
    sb = consumption_summary(b.db, b.vehicle, ms, me, None)
    assert sb["charged_kwh"] == 55 and not sb["has_fuel"]

    pdf = build_month_pdf(a.ctx, 2026, 10)
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000
    assert build_year_pdf(a.ctx, 2026).startswith(b"%PDF")

    wb = load_workbook(io.BytesIO(build_xlsx(a.ctx, 2026)))
    ws = wb["Kulut"]
    values = [[c.value for c in row] for row in ws.iter_rows()]
    flat = [v for row in values for v in row if v is not None]
    assert "Yksikköhinta" in flat and "Kulu-ID" in flat
    assert 55 not in flat  # toisen auton lataus ei näy
    wb_b = load_workbook(io.BytesIO(build_xlsx(b.ctx, 2026)))
    assert "Tankkaus" not in [c.value for row in wb_b["Kulut"].iter_rows() for c in row]


def test_unit_price_optional_and_excel_roundtrip(tmp_path):
    a = make_service(tmp_path, "530e")
    a.db.insert_trip(trip(5, 8, 10000, 100))
    e1 = a.on_consumption(_ev(10, 5, 20))
    e2 = a.on_consumption(_ev(10, 7, 12, kind="refuel", qty=33, source="tank_level"))
    assert a.db.get_consumption(e1)["unit_price"] is None
    a.update_consumption(e1, "unit_price", "0,089")
    assert a.db.get_consumption(e1)["unit_price"] == 0.089
    data = build_xlsx(a.ctx, 2026)
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Kulut"]
    header_row = next(r for r in range(1, 10) if "Kulu-ID" in [c.value for c in ws[r]])
    cols = {c.value: c.column for c in ws[header_row] if c.value}
    for r in range(header_row + 1, ws.max_row + 1):
        if ws.cell(row=r, column=cols["Kulu-ID"]).value == e2:
            ws.cell(row=r, column=cols["Yksikköhinta"]).value = "1,899"
            ws.cell(row=r, column=cols["Kommentti"]).value = "Neste"
        if ws.cell(row=r, column=cols["Kulu-ID"]).value == e1:
            ws.cell(row=r, column=cols["Yksikköhinta"]).value = None  # hinta poistetaan
    buf = io.BytesIO()
    wb.save(buf)
    res = a.import_bytes(buf.getvalue(), "kulut.xlsx")
    assert a.db.get_consumption(e2)["unit_price"] == 1.899
    assert a.db.get_consumption(e2)["comment"] == "Neste"
    assert a.db.get_consumption(e1)["unit_price"] is None
    assert any("Kulu" in x for x in res.applied)


def test_quantity_correction_needs_reason_and_lock_blocks(tmp_path):
    import pytest

    from logbook.database import LogbookError

    a = make_service(tmp_path, "530e")
    a.db.insert_trip(trip(5, 8, 10000, 100))
    e1 = a.on_consumption(_ev(10, 5, 20))
    with pytest.raises(LogbookError):
        a.update_consumption(e1, "quantity", "12")
    a.update_consumption(e1, "quantity", "12", reason="Laturin kuitti")
    ev = a.db.get_consumption(e1)
    assert ev["quantity"] == 12 and ev["orig_quantity"] == 10 and ev["corrected"] == 1
    a.lock_period(2026, 10, now=T0 + timedelta(days=60))
    with pytest.raises(LogbookError):
        a.update_consumption(e1, "quantity", "13", reason="x")
    a.update_consumption(e1, "unit_price", "0,1")  # hinnan voi lisätä myös lukituksen jälkeen
    assert any(ch["field"] == f"consumption:{e1}:unit_price" for ch in a.db.history())


def test_costs_page_and_inline_price(tmp_path):
    from fastapi.testclient import TestClient

    from logbook.main import LogbookApp
    from logbook.web_ui import create_app

    s = settings(export_dir=str(tmp_path / "share"))
    s.data_dir = str(tmp_path / "data")
    app = LogbookApp(s)
    a = app.services["530e"]
    eid = a.on_consumption(_ev(10, 5, 20, soc_start=5, soc_end=100, address="Kotikatu 1"))
    app.services["ix3"].on_consumption(_ev(10, 5, 21, qty=55, address="iX3-laturi"))
    c = TestClient(create_app(app, restrict=False))
    r = c.get("/v/530e/costs?year=2026&month=10")
    assert r.status_code == 200 and "Kotikatu 1" in r.text and "iX3-laturi" not in r.text and "Tankattu" in r.text
    r = c.get("/v/ix3/costs?year=2026&month=10")
    assert r.status_code == 200 and "iX3-laturi" in r.text and "Tankattu" not in r.text
    r = c.post(f"/v/530e/costs/{eid}/field", json={"field": "unit_price", "value": "0,0899"})
    assert r.status_code == 200 and r.json()["event"]["unit_price"] == 0.0899
    r = c.post(f"/v/530e/costs/{eid}/field", json={"field": "unit_price", "value": "abc"})
    assert r.status_code == 400
    # toisen auton kulua ei voi muokata tämän auton polun kautta
    r = c.post("/v/530e/costs/99/field", json={"field": "comment", "value": "x"})
    assert r.status_code == 400
    r = c.post(
        "/v/530e/costs/new",
        data={"kind": "refuel", "start_time": "2026-10-09T12:00", "quantity": "40,5", "reason": "kuitti"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    r = c.post("/v/ix3/costs/new", data={"kind": "refuel", "start_time": "2026-10-09T12:00", "quantity": "4"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert len(a.db.list_consumption()) == 2


def test_shared_charger_entity_routes_to_each_vehicle(tmp_path):
    from logbook.main import LogbookApp

    s = settings(export_dir=str(tmp_path / "share"))
    s.data_dir = str(tmp_path / "data")
    for vid in ("530e", "ix3"):
        s.vehicle(vid).entities["charger_energy"] = ABB
    app = LogbookApp(s)
    app.entity_map = {ABB: [("530e", "charger_energy"), ("ix3", "charger_energy")]}
    st = {"state": "1500", "attributes": {"unit_of_measurement": "Wh"}, "last_updated": "2026-11-21T19:00:00+00:00"}
    app._route(ABB, st)
    assert app.services["530e"].consumption.s.counter == 1.5
    assert app.services["ix3"].consumption.s.counter == 1.5
    # ajotunnistus ei saa kulurooleja
    assert app.services["530e"].trip_manager.ignored_duplicates == 0


def _work_day(sim):
    """25.11.2026: kotona yön yli kytkettynä, aamulla ajoon ilman irrotustietoa, lataus työpaikalla."""
    sim.at(1).ev("soc", 4.7).wh(0).ev("charging_method", "AC_TYPE2PLUG")
    sim.at(300).wh(462)
    sim.at(308).wh(0)  # kaapeli irti kotilaturista, auto ei ilmoita
    sim.at(313).ev("location", "not_home", {"latitude": HOME[0] + 0.1, "longitude": HOME[1]})
    sim.at(335).ev("soc", 10.2)
    sim.at(453).ev("charging_state", "CHARGINGACTIVE").ev("soc", 10.3)
    sim.at(543).ev("soc", 68.5)
    sim.at(633).ev("soc", 100).ev("charging_state", "CHARGINGENDED")
    sim.at(704).ev("soc", 96.4).ev("charging_state", "NOCHARGING").ev("charging_method", "NOCHARGING")


def test_away_charge_after_driving_off_with_plug_flag_on_is_detected():
    sim = CSim()
    _work_day(sim)
    assert [e["source"] for e in sim.events] == ["charger", "estimate"]
    home, work = sim.events
    assert home["quantity"] == 0.46 and home["at_home"] == 1
    assert work["at_home"] == 0 and work["soc_start"] == 10.2 and work["soc_end"] == 96.4
    assert work["quantity"] == round(89.8 * 0.12, 2)
    assert work["start_time"] == timeutil.to_iso(T0 + timedelta(minutes=453))


def test_charging_state_does_not_split_running_plug():
    sim = CSim()
    sim.at(1).ev("soc", 20).wh(0).ev("charging_method", "AC_TYPE2PLUG")
    sim.at(5).ev("charging_state", "CHARGINGACTIVE")
    sim.at(30).wh(3000)
    sim.at(40).ev("charging_state", "CHARGINGENDED")
    sim.at(50).ev("charging_state", "CHARGINGACTIVE")
    sim.at(90).ev("charging_method", "NOCHARGING")
    assert len(sim.events) == 1 and sim.events[0]["quantity"] == 3.0


def test_recover_charges_adds_only_missing():
    from logbook.consumption import recover_charges

    s, v, _ = cfg()
    rec = []

    class Rec:
        def __init__(self):
            self.t = T0

        def at(self, minutes):
            self.t = T0 + timedelta(minutes=minutes)
            return self

        def ev(self, role, state, attrs=None):
            rec.append((self.t, role, str(state), attrs or {}))
            return self

        def wh(self, value):
            return self.ev("charger_energy", value, {"unit_of_measurement": "Wh"})

    r = Rec()
    r.at(0).ev("location", "home", {"latitude": HOME[0], "longitude": HOME[1]})
    _work_day(r)
    zones = [Zone("zone.home", HOME[0], HOME[1], 100)]
    # ikkuna alkaa kesken kotikytkennän: se ohitetaan, työpaikan lataus palautetaan
    out = recover_charges(v, s, rec, T0 + timedelta(minutes=1), [], zones=zones)
    assert len(out) == 1 and out[0]["source"] == "estimate" and out[0]["at_home"] == 0
    assert "historiasta" in out[0]["note"]
    # jo kirjattu (esim. käsin) lataus samalla ajalla estää palautuksen
    manual = {"kind": "charge", "start_time": timeutil.to_iso(T0 + timedelta(minutes=500)), "end_time": None}
    assert recover_charges(v, s, rec, T0 + timedelta(minutes=1), [manual], zones=zones) == []
