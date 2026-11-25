import asyncio
import json

from fastapi.testclient import TestClient
from helpers import settings

from logbook.discovery import discover, find_stream_status, powertrain_from_drive_train, suggest_id
from logbook.main import LogbookApp
from logbook.vehicle_registry import migrate_options, parse_settings
from logbook.vehicle_store import VehicleStore
from logbook.web_ui import create_app

VIN_A = "WBATEST0000000001"
VIN_B = "WBYTEST0000000002"


def reg(eid, vin, key, **extra):
    return {"entity_id": eid, "unique_id": f"{vin}_{key}", "platform": "cardata", "disabled_by": None, **extra}


REGISTRY = [
    reg("device_tracker.phev_location", VIN_A, "device_tracker"),
    reg("sensor.phev_mileage", VIN_A, "vehicle.vehicle.travelledDistance"),
    reg("binary_sensor.phev_motion", VIN_A, "vehicle.isMoving"),
    reg("sensor.phev_soc", VIN_A, "vehicle.powertrain.electric.battery.stateOfCharge.displayed"),
    reg("sensor.charging_ev_charging_method", VIN_A, "vehicle.drivetrain.electricEngine.charging.method"),
    reg("sensor.phev_tank", VIN_A, "vehicle.drivetrain.fuelSystem.remainingFuel"),
    reg("sensor.phev_metadata", VIN_A, "diagnostics_vehicle_metadata"),
    reg("device_tracker.bev_location", VIN_B, "device_tracker"),
    reg("sensor.bev_mileage", VIN_B, "vehicle.vehicle.travelledDistance"),
    reg("sensor.charging_ev_charging_method_2", VIN_B, "vehicle.drivetrain.electricEngine.charging.method"),
    reg("sensor.bev_metadata", VIN_B, "diagnostics_vehicle_metadata"),
    reg("sensor.bev_disabled", VIN_B, "vehicle.isMoving", disabled_by="user"),
    {"entity_id": "sensor.other", "unique_id": f"{VIN_B}_vehicle.isMoving", "platform": "other"},
    {"entity_id": "sensor.cardata_stream", "unique_id": "01ABC_diagnostics_connection_status", "platform": "cardata"},
]
STATES = {
    "sensor.phev_metadata": {"attributes": {"vehicle_basic_data": {"model_name": "530e xDrive", "series_development": "G30", "drive_train": "PHEV"}, "vehicle_basic_data_raw": {"brand": "BMW"}}},
    "sensor.bev_metadata": {"attributes": {"vehicle_basic_data": {"model_name": "iX3", "series_development": "NA5", "drive_train": "BEV"}, "vehicle_basic_data_raw": {"brand": "BMW"}}},
}


def test_discover_by_vin():
    found = discover(REGISTRY, STATES)
    assert set(found) == {VIN_A, VIN_B}
    a, b = found[VIN_A], found[VIN_B]
    assert a.entities["location"] == "device_tracker.phev_location"
    assert a.entities["odometer"] == "sensor.phev_mileage"
    assert a.entities["fuel_level"] == "sensor.phev_tank"
    assert a.powertrain == "PHEV" and a.model == "530e xDrive" and a.brand == "BMW"
    # latausentiteetti ilman auton nimeä kuuluu oikealle VIN:lle
    assert b.entities["charging_method"] == "sensor.charging_ev_charging_method_2"
    assert "motion" not in b.entities  # poistettu käytöstä / eri integraatio
    assert b.powertrain == "BEV" and b.display_name == "BMW iX3 (NA5)"
    assert find_stream_status(REGISTRY) == "sensor.cardata_stream"
    assert suggest_id(b, set()) == "ix3"
    assert suggest_id(a, {"530e"}) == "530e_2"


def test_powertrain_mapping():
    assert powertrain_from_drive_train("PHEV") == "PHEV"
    assert powertrain_from_drive_train("BEV") == "BEV"
    assert powertrain_from_drive_train("MHEV") == "ICE"
    assert powertrain_from_drive_train("COMBUSTION") == "ICE"
    assert powertrain_from_drive_train("") == ""


V02_OPTIONS = {
    "vehicles": [
        {
            "id": "530e",
            "plate": "ABC-123",
            "make": "BMW",
            "model": "530e xDrive (G30)",
            "powertrain": "PHEV",
            "vin": "",
            "driver": "Kuljettaja",
            "enabled": True,
            "charging_pause_enabled": True,
            "charging_pause_timeout_s": 2700,
            "entity_location": "device_tracker.phev_location",
            "entity_odometer": "sensor.phev_mileage",
            "entity_motion": "binary_sensor.phev_motion",
            "entity_soc": "sensor.phev_soc",
            "entity_charging_state": "sensor.charging_ev_charging_state",
            "entity_charging_method": "sensor.charging_ev_charging_method",
            "entity_hv_system_state": "sensor.charging_ev_hv_system_state",
            "entity_fuel_level": "sensor.phev_tank",
            "entity_charger_energy": "sensor.home_charger_energy",
            "charge_kwh_per_pct": 0.12,
        },
        {
            "id": "ix3",
            "plate": "",
            "make": "BMW",
            "model": "iX3 (NA5)",
            "powertrain": "BEV",
            "vin": "",
            "driver": "",
            "enabled": False,
            "charging_pause_enabled": True,
            "charging_pause_timeout_s": 5400,
            "entity_location": "",
            "entity_odometer": "",
            "entity_charger_energy": "",
            "charge_kwh_per_pct": 0.8,
        },
    ],
    "export_dir": "/share/x",
}


def test_migration_keeps_every_entity(tmp_path):
    legacy = parse_settings(V02_OPTIONS, data_dir=str(tmp_path))  # sama polku kuin ennen tiedostoa
    store = VehicleStore(tmp_path).load_or_migrate(V02_OPTIONS)
    assert (tmp_path / "vehicles.json").exists()
    assert store["home_charger_entity"] == "sensor.home_charger_energy"
    s = parse_settings(V02_OPTIONS, data_dir=str(tmp_path), store=json.loads((tmp_path / "vehicles.json").read_text()))
    v = s.vehicle("530e")
    for role, eid in legacy.vehicle("530e").entities.items():
        assert v.entity(role) == eid, role
    assert v.entity("charger_energy") == "sensor.home_charger_energy"
    assert v.entity_source("charger_energy") == "charger"
    assert v.entity_source("location") == "manual"
    assert (v.plate, v.driver, v.charging_pause_timeout_s, v.charge_kwh_per_pct) == ("ABC-123", "Kuljettaja", 2700, 0.12)
    ix3 = s.vehicle("ix3")
    assert not ix3.enabled and not ix3.use_home_charger
    assert not s.vehicle_errors
    # toinen käynnistys lukee tiedoston eikä siirrä uudelleen
    assert VehicleStore(tmp_path).load_or_migrate({"vehicles": []})["vehicles"][0]["id"] == "530e"


def test_migration_from_helper_settings():
    store = migrate_options({"vehicles": [{"id": "x1", "powertrain": "BEV", "entity_location": "a", "entity_odometer": "b"}]})
    assert store["home_charger_entity"] == ""
    assert store["vehicles"][0]["entities"] == {"location": "a", "odometer": "b"}


def test_powertrain_roles_and_shared_charger():
    store = {
        "home_charger_entity": "sensor.home",
        "vehicles": [
            {"id": "phev", "powertrain": "PHEV", "vin": VIN_A, "use_home_charger": True},
            {"id": "bev", "powertrain": "BEV", "vin": VIN_B, "use_home_charger": True},
            {"id": "ice", "powertrain": "ICE", "fuel_type": "diesel", "entities": {"location": "x", "odometer": "y", "soc": "z"}},
        ],
    }
    s = parse_settings({}, store=store)
    assert not s.vehicle_errors  # VIN:llä varustetuilta puuttuvat entiteetit löytyvät myöhemmin
    phev, bev, ice = s.vehicle("phev"), s.vehicle("bev"), s.vehicle("ice")
    assert phev.charger_shared and bev.charger_shared and not ice.charger_shared
    assert ice.entity("soc") == "" and "charger_energy" not in ice.entities
    assert ice.has_fuel and not ice.has_charging and ice.powertrain_label == "Polttomoottori (ICE), diesel"
    found = discover(REGISTRY, STATES)
    bev.apply_discovered(found[VIN_B].entities)
    assert bev.entity("location") == "device_tracker.bev_location"
    assert bev.entity("fuel_level") == ""  # BEV ei seuraa polttoainetta
    bev.overrides["location"] = "device_tracker.manual"
    bev.resolve()
    assert bev.entity("location") == "device_tracker.manual"


def test_shared_charger_goes_to_car_whose_soc_rose():
    from datetime import datetime, timedelta, timezone

    from logbook.consumption import ConsumptionTracker
    from logbook.trip_manager import Zone

    store = {
        "home_charger_entity": "sensor.home",
        "vehicles": [
            {"id": "a", "powertrain": "PHEV", "use_home_charger": True, "entities": {"location": "l1", "odometer": "o1"}},
            {"id": "b", "powertrain": "BEV", "use_home_charger": True, "entities": {"location": "l2", "odometer": "o2"}},
        ],
    }
    s = parse_settings({}, store=store)
    zones = [Zone("zone.home", 61.0, 23.0, 200)]
    got = {"a": [], "b": []}
    trackers = {vid: ConsumptionTracker(s.vehicle(vid), s, on_charge=got[vid].append, zones=zones) for vid in ("a", "b")}
    t0 = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
    for vid, ct in trackers.items():
        ct.process("location", "home", {"latitude": 61.0, "longitude": 23.0}, t0)
        ct.process("soc", "50", {}, t0)
        ct.process("charger_energy", "0", {"unit_of_measurement": "kWh"}, t0)
        ct.process("charging_method", "AC_TYPE2PLUG", {}, t0 + timedelta(minutes=1))
    for i in range(1, 6):
        t = t0 + timedelta(minutes=10 * i)
        for ct in trackers.values():
            ct.process("charger_energy", str(2.0 * i), {"unit_of_measurement": "kWh"}, t)
        trackers["a"].process("soc", str(50 + 8 * i), {}, t)
    t_end = t0 + timedelta(hours=2)
    for ct in trackers.values():
        ct.process("charging_method", "NOCHARGING", {}, t_end)
    assert [e["quantity"] for e in got["a"]] == [10.0]
    assert got["b"] == []


class FakeHA:
    def __init__(self):
        self.states = {}
        self.closed = asyncio.Event()

    async def get_states(self):
        return [{"entity_id": k, "state": "on", "attributes": v.get("attributes", {}), "last_updated": "2026-11-20T10:00:00+00:00"} for k, v in STATES.items()] + [
            {"entity_id": r["entity_id"], "state": "1", "attributes": {}, "last_updated": "2026-11-20T10:00:00+00:00"} for r in REGISTRY if r["entity_id"] not in STATES
        ]

    async def entity_registry(self):
        return REGISTRY

    async def subscribe_entities(self, eids, cb):
        self.subscribed = eids

    async def history(self, *a, **k):
        return {}


def test_app_discovers_and_fills_vin(tmp_path):
    VehicleStore(tmp_path).load_or_migrate(V02_OPTIONS)
    store = VehicleStore(tmp_path)
    s = parse_settings(V02_OPTIONS, data_dir=str(tmp_path), store=store.load())
    s.export_dir = str(tmp_path / "share")
    app = LogbookApp(s, store=store)
    ha = FakeHA()
    asyncio.run(app._on_connected(ha))
    v = s.vehicle("530e")
    assert v.vin == VIN_A  # täydennetty, koska omat entiteetit kuuluvat tähän autoon
    assert store.load()["vehicles"][0]["vin"] == VIN_A
    assert v.entity("location") == "device_tracker.phev_location"
    assert not app.services["530e"].entity_status["_not_configured"]
    assert set(app.discovered) == {VIN_A, VIN_B}
    assert "sensor.home_charger_energy" not in ha.subscribed  # ei HA:ssa: ei tilata


def test_vehicles_page_add_and_edit(tmp_path):
    VehicleStore(tmp_path).load_or_migrate(V02_OPTIONS)
    store = VehicleStore(tmp_path)
    s = parse_settings(V02_OPTIONS, data_dir=str(tmp_path), store=store.load())
    s.export_dir = str(tmp_path / "share")
    app = LogbookApp(s, store=store)
    asyncio.run(app._on_connected(FakeHA()))
    app.ha_connected = True
    c = TestClient(create_app(app, restrict=False))
    r = c.get("/vehicles")
    assert r.status_code == 200 and VIN_B in r.text and "Löydetyt autot" in r.text
    r = c.get(f"/vehicles?add={VIN_B}")
    assert 'value="ix3"' in r.text and "BMW" in r.text
    # tunniste ix3 on jo käytössä (vanha iX3-paikka), joten ehdotus on ix3_2
    assert 'value="ix3_2"' in r.text
    # vanhaan iX3-paikkaan liitetään VIN ja otetaan käyttöön
    form = {
        "orig_id": "ix3", "id": "ix3", "plate": "xyz-789", "make": "BMW", "model": "iX3 (NA5)", "powertrain": "BEV",
        "fuel_type": "", "vin": VIN_B, "driver": "K", "enabled": "1", "charging_pause_min": "90",
        "charge_kwh_per_pct": "0,8", "use_home_charger": "1", "auto_discover": "1",
    }
    r = c.post("/vehicles/save", data=form, follow_redirects=False)
    assert r.status_code == 303 and "err" not in r.headers["location"], r.headers["location"]
    saved = [v for v in store.load()["vehicles"] if v["id"] == "ix3"][0]
    assert saved["plate"] == "XYZ-789" and saved["vin"] == VIN_B and saved["enabled"]
    assert saved["charging_pause_timeout_s"] == 5400 and saved["charge_kwh_per_pct"] == 0.8
    assert saved["entities"] == {}
    assert app.restart_needed
    # sama VIN toiselle autolle hylätään
    bad = dict(form, orig_id="", id="kopio")
    r = c.post("/vehicles/save", data=bad, follow_redirects=False)
    assert "err" in r.headers["location"]
    # auto ilman löydettyjä entiteettejä hylätään
    r = c.post("/vehicles/save", data=dict(form, orig_id="", id="muu", vin=""), follow_redirects=False)
    assert "err" in r.headers["location"]
    r = c.post("/vehicles/charger", data={"home_charger_entity": "sensor.uusi"}, follow_redirects=False)
    assert store.load()["home_charger_entity"] == "sensor.uusi"
    assert c.get("/settings").status_code == 200


def test_legacy_settings_pages_still_render(tmp_path):
    s = settings(export_dir=str(tmp_path / "share"))
    s.data_dir = str(tmp_path / "data")
    app = LogbookApp(s)
    c = TestClient(create_app(app, restrict=False))
    assert c.get("/vehicles").status_code == 200
    assert c.get("/").status_code == 200


def test_transfer_package_roundtrip(tmp_path):
    from logbook.transfer import export_package, import_package_if_fresh, package_path

    old = tmp_path / "old"
    share = tmp_path / "share"
    VehicleStore(old).load_or_migrate(V02_OPTIONS)
    s = parse_settings(V02_OPTIONS, data_dir=str(old), store=VehicleStore(old).load())
    s.export_dir = str(share)
    app = LogbookApp(s, store=VehicleStore(old))
    from test_logbook import trip

    app.services["530e"].db.insert_trip(trip(1, 6, 10000, 15, "business", "Asiakas"))
    (old / "options.json").write_text("{}")
    export_package(old, share, app.services)
    assert package_path(share).exists()
    new = tmp_path / "new"
    assert import_package_if_fresh(new, share)
    assert not package_path(share).exists()
    assert (new / "vehicles.json").exists() and not (new / "options.json").exists()
    s2 = parse_settings({}, data_dir=str(new), store=VehicleStore(new).load())
    s2.export_dir = str(share)
    app2 = LogbookApp(s2, store=VehicleStore(new))
    assert len(app2.services["530e"].db.query("SELECT id FROM trips")) == 1
    # olemassa olevan asennuksen päälle ei tuoda
    export_package(old, share)
    assert not import_package_if_fresh(new, share)


def test_restore_from_backups(tmp_path):
    """Vanhan asennuksen raporttikansion varmuuskopioista palautus, kun siirtopakettia ei ole."""
    from logbook.transfer import import_backups_if_fresh
    from test_logbook import trip

    old = tmp_path / "old"
    share = tmp_path / "share"
    VehicleStore(old).load_or_migrate(V02_OPTIONS)
    s = parse_settings(V02_OPTIONS, data_dir=str(old), store=VehicleStore(old).load())
    s.export_dir = str(share)
    app = LogbookApp(s, store=VehicleStore(old))
    app.services["530e"].db.insert_trip(trip(1, 6, 10000, 15, "business", "Asiakas"))
    app.services["530e"].backup_db()
    new = tmp_path / "new"
    assert import_backups_if_fresh(new, share) == ["530e"]
    stored = VehicleStore(new).load()["vehicles"][0]
    assert (stored["id"], stored["plate"], stored["powertrain"], stored["driver"]) == ("530e", "ABC-123", "PHEV", "Kuljettaja")
    s2 = parse_settings({}, data_dir=str(new), store=VehicleStore(new).load())
    assert not s2.vehicle_errors
    s2.export_dir = str(share)
    app2 = LogbookApp(s2, store=VehicleStore(new))
    assert len(app2.services["530e"].db.query("SELECT id FROM trips")) == 1
    # VIN puuttuu: täydennetään yksiselitteisen mallin perusteella ja entiteetit löytyvät
    asyncio.run(app2._on_connected(FakeHA()))
    v = s2.vehicle("530e")
    assert v.vin == VIN_A and v.entity("odometer") == "sensor.phev_mileage"
    assert not app2.services["530e"].entity_status["_not_configured"]
    # toista kertaa ei palauteta
    assert import_backups_if_fresh(new, share) == []
