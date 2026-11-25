"""Asetusten luku add-onin asetussivulta (/data/options.json) ja autojen tarkistus."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_LOG = logging.getLogger(__name__)

ROLES = (
    "location",
    "odometer",
    "motion",
    "soc",
    "charging_state",
    "charging_method",
    "hv_system_state",
    "fuel_level",
    "charger_energy",
)
# Kululaskennan roolit: näitä ei syötetä ajotunnistukselle
CONSUMPTION_ROLES = ("fuel_level", "charger_energy")
REQUIRED_ROLES = ("location", "odometer")
CHARGING_ROLES = ("soc", "charging_state", "charging_method", "hv_system_state", "charger_energy")
FUEL_ROLES = ("fuel_level",)

POWERTRAINS = ("BEV", "PHEV", "ICE")
POWERTRAIN_LABELS = {"BEV": "Täyssähkö (BEV)", "PHEV": "Ladattava hybridi (PHEV)", "ICE": "Polttomoottori (ICE)"}
FUEL_TYPES = ("", "petrol", "diesel")
FUEL_TYPE_LABELS = {"": "", "petrol": "bensiini", "diesel": "diesel"}

VEHICLE_ID_RE = re.compile(r"^[a-z0-9_]{2,16}$")
VIN_RE = re.compile(r"^$|^[A-HJ-NPR-Z0-9]{17}$")

DEFAULT_PAUSE_TIMEOUT = {"PHEV": 2700, "BEV": 5400, "ICE": 0}
# Arviokerroin (kWh seinästä / varaus-%) muualla kuin kotilaturilla ladattaessa
DEFAULT_KWH_PER_PCT = {"PHEV": 0.12, "BEV": 0.0, "ICE": 0.0}

STORE_VERSION = 1


@dataclass
class VehicleConfig:
    id: str
    plate: str = ""
    make: str = "BMW"
    model: str = ""
    powertrain: str = "PHEV"
    fuel_type: str = ""
    vin: str = ""
    driver: str = ""
    enabled: bool = True
    charging_pause_enabled: bool = True
    charging_pause_timeout_s: int = 2700
    charge_kwh_per_pct: float = 0.0
    # Käytössä olevat entiteetit roolettain (käsin asetetut + kotilaturi + löydetyt)
    entities: dict[str, str] = field(default_factory=dict)
    # Käsin asetetut entiteetit; nämä voittavat automaattisesti löydetyt
    overrides: dict[str, str] = field(default_factory=dict)
    # CarData-integraatiosta VIN:llä löydetyt entiteetit
    discovered: dict[str, str] = field(default_factory=dict)
    auto_discover: bool = True
    use_home_charger: bool = False
    home_charger: str = ""
    # Sama laturi on usealla autolla: mitattu energia kuuluu vain autolle, jonka varaus nousi
    charger_shared: bool = False

    @property
    def display_name(self) -> str:
        return f"{self.make} {self.model}".strip()

    @property
    def powertrain_label(self) -> str:
        label = POWERTRAIN_LABELS.get(self.powertrain, self.powertrain)
        fuel = FUEL_TYPE_LABELS.get(self.fuel_type, "")
        return f"{label}, {fuel}" if fuel and self.has_fuel else label

    @property
    def folder_name(self) -> str:
        """Auton kansio raporttikansiossa: <id>_<rekisteritunnus>."""
        plate = safe_name(self.plate)
        return f"{self.id}_{plate}" if plate else self.id

    @property
    def file_prefix(self) -> str:
        plate = safe_name(self.plate)
        return f"Ajopaivakirja_{self.id}_{plate}" if plate else f"Ajopaivakirja_{self.id}"

    def entity(self, role: str) -> str:
        return self.entities.get(role, "") or ""

    @property
    def has_fuel(self) -> bool:
        return self.powertrain in ("PHEV", "ICE")

    @property
    def has_charging(self) -> bool:
        return self.powertrain in ("PHEV", "BEV")

    def role_allowed(self, role: str) -> bool:
        if role in FUEL_ROLES:
            return self.has_fuel
        if role in CHARGING_ROLES:
            return self.has_charging
        return True

    def entity_source(self, role: str) -> str:
        """Mistä roolin entiteetti tulee: 'manual', 'charger', 'auto' tai ''."""
        if self.overrides.get(role):
            return "manual"
        if role == "charger_energy" and self.use_home_charger and self.home_charger:
            return "charger"
        if self.auto_discover and self.discovered.get(role):
            return "auto"
        return ""

    def resolve(self) -> None:
        """Laskee käytössä olevat entiteetit: käsin asetettu > kotilaturi > löydetty."""
        out: dict[str, str] = {}
        for role in ROLES:
            if not self.role_allowed(role):
                continue
            src = self.entity_source(role)
            if src == "manual":
                out[role] = self.overrides[role]
            elif src == "charger":
                out[role] = self.home_charger
            elif src == "auto":
                out[role] = self.discovered[role]
            else:
                out[role] = ""
        self.entities = out

    def apply_discovered(self, entities: dict[str, str]) -> None:
        self.discovered = {k: v for k, v in (entities or {}).items() if v}
        self.resolve()

    def entity_ids(self) -> list[str]:
        return [e for e in self.entities.values() if e]


@dataclass
class Settings:
    vehicles: list[VehicleConfig] = field(default_factory=list)
    home_charger_entity: str = ""
    vehicle_errors: dict[str, list[str]] = field(default_factory=dict)
    export_dir: str = "/share/ajopaivakirja"
    data_dir: str = "/data"
    month_folder_format: str = "%m"
    trip_start_distance_m: int = 100
    trip_end_timeout_s: int = 600
    ghost_motion_max_s: int = 90
    home_zones: list[str] = field(default_factory=lambda: ["zone.home"])
    stream_status_entity: str = "sensor.cardata_debug_device_stream_connection_status"
    backfill_max_hours: int = 24
    ha_mqtt_enabled: bool = True
    report_xlsx: bool = True
    report_csv: bool = False
    xlsx_import_poll_s: int = 60
    odometer_gap_tolerance_km: float = 1.0
    auto_lock_after_days: int = 15
    commute_counts_as_private: bool = True
    archive_filtered: list[str] = field(default_factory=lambda: ["business"])
    nominatim_user_agent: str = "cardata-logbook"
    refuel_min_l: float = 3.0
    log_level: str = "info"

    def vehicle(self, vehicle_id: str) -> VehicleConfig | None:
        for v in self.vehicles:
            if v.id == vehicle_id:
                return v
        return None

    def active_vehicles(self) -> list[VehicleConfig]:
        return [v for v in self.vehicles if v.enabled and v.id not in self.vehicle_errors]


def safe_name(value: str) -> str:
    """Tiedostonimeen kelpaava muoto (rekisteritunnus ABC-123 säilyy ennallaan)."""
    return re.sub(r"[^A-Za-z0-9ÅÄÖåäö_-]", "", (value or "").strip().upper())


def migrate_options(options: dict) -> dict:
    """Muuntaa v0.2:n asetussivun autolistan (options.vehicles) autotiedostoksi.

    Kaikki nykyiset entiteetit säilyvät käsin asetettuina, joten auto käyttää täsmälleen
    samoja entiteettejä kuin ennen. Ensimmäinen kotilaturin energia-entiteetti siirtyy
    yhteiseksi kotilaturiksi.
    """
    raws = list(options.get("vehicles") or [])
    home = ""
    for raw in raws:
        home = str(raw.get("entity_charger_energy") or "").strip()
        if home:
            break
    vehicles = []
    for raw in raws:
        powertrain = str(raw.get("powertrain") or "PHEV").upper()
        overrides = {}
        for role in ROLES:
            val = str(raw.get(f"entity_{role}") or "").strip()
            if val and not (role == "charger_energy" and val == home):
                overrides[role] = val
        charger = str(raw.get("entity_charger_energy") or "").strip()
        timeout = raw.get("charging_pause_timeout_s")
        kwh_pct = raw.get("charge_kwh_per_pct")
        vehicles.append(
            {
                "id": str(raw.get("id", "")).strip(),
                "plate": str(raw.get("plate") or "").strip().upper(),
                "make": str(raw.get("make") or "BMW").strip(),
                "model": str(raw.get("model") or "").strip(),
                "powertrain": powertrain,
                "fuel_type": "",
                "vin": str(raw.get("vin") or "").strip().upper(),
                "driver": str(raw.get("driver") or "").strip(),
                "enabled": bool(raw.get("enabled", True)),
                "charging_pause_enabled": bool(raw.get("charging_pause_enabled", True)),
                "charging_pause_timeout_s": int(timeout if timeout is not None else DEFAULT_PAUSE_TIMEOUT.get(powertrain, 2700)),
                "charge_kwh_per_pct": float(kwh_pct if kwh_pct is not None else DEFAULT_KWH_PER_PCT.get(powertrain, 0.0)),
                "auto_discover": True,
                "use_home_charger": bool(home) and charger == home,
                "entities": overrides,
            }
        )
    return {"version": STORE_VERSION, "home_charger_entity": home, "vehicles": vehicles}


def vehicle_to_store(v: VehicleConfig) -> dict:
    return {
        "id": v.id,
        "plate": v.plate,
        "make": v.make,
        "model": v.model,
        "powertrain": v.powertrain,
        "fuel_type": v.fuel_type,
        "vin": v.vin,
        "driver": v.driver,
        "enabled": v.enabled,
        "charging_pause_enabled": v.charging_pause_enabled,
        "charging_pause_timeout_s": v.charging_pause_timeout_s,
        "charge_kwh_per_pct": v.charge_kwh_per_pct,
        "auto_discover": v.auto_discover,
        "use_home_charger": v.use_home_charger,
        "entities": {k: val for k, val in v.overrides.items() if val},
    }


def settings_to_store(s: "Settings") -> dict:
    return {
        "version": STORE_VERSION,
        "home_charger_entity": s.home_charger_entity,
        "vehicles": [vehicle_to_store(v) for v in s.vehicles],
    }


def vehicle_from_store(raw: dict, home_charger: str = "") -> VehicleConfig:
    powertrain = str(raw.get("powertrain") or "PHEV").upper()
    timeout = raw.get("charging_pause_timeout_s")
    kwh_pct = raw.get("charge_kwh_per_pct")
    overrides = {role: str(val).strip() for role, val in (raw.get("entities") or {}).items() if role in ROLES and str(val or "").strip()}
    v = VehicleConfig(
        id=str(raw.get("id", "")).strip(),
        plate=str(raw.get("plate") or "").strip().upper(),
        make=str(raw.get("make") or "").strip(),
        model=str(raw.get("model") or "").strip(),
        powertrain=powertrain,
        fuel_type=str(raw.get("fuel_type") or "").strip().lower(),
        vin=str(raw.get("vin") or "").strip().upper(),
        driver=str(raw.get("driver") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        charging_pause_enabled=bool(raw.get("charging_pause_enabled", True)),
        charging_pause_timeout_s=int(timeout if timeout is not None else DEFAULT_PAUSE_TIMEOUT.get(powertrain, 2700)),
        charge_kwh_per_pct=float(kwh_pct if kwh_pct is not None else DEFAULT_KWH_PER_PCT.get(powertrain, 0.0)),
        overrides=overrides,
        auto_discover=bool(raw.get("auto_discover", True)),
        use_home_charger=bool(raw.get("use_home_charger", False)),
        home_charger=home_charger,
    )
    v.resolve()
    return v


def validate_vehicle(v: VehicleConfig) -> list[str]:
    errors: list[str] = []
    if not VEHICLE_ID_RE.match(v.id):
        errors.append(f"Virheellinen auton tunniste '{v.id}' (sallittu a-z, 0-9, _; 2-16 merkkiä).")
    if not VIN_RE.match(v.vin):
        errors.append("VIN on virheellinen (17 merkkiä, ei I, O tai Q).")
    if v.powertrain not in POWERTRAINS:
        errors.append("Käyttövoiman on oltava BEV, PHEV tai ICE.")
    if v.fuel_type not in FUEL_TYPES:
        errors.append("Polttoaineen on oltava bensiini tai diesel.")
    if v.overrides.get("fuel_level") and not v.has_fuel:
        errors.append("Polttoaine-entiteetti on asetettu sähköautolle (BEV).")
    if v.enabled:
        for role in REQUIRED_ROLES:
            # VIN:llä varustetun auton entiteetit voivat löytyä vasta HA-yhteyden auettua
            if not v.entity(role) and not (v.auto_discover and v.vin):
                errors.append(f"Pakollinen entiteetti '{role}' puuttuu.")
    return errors


def mark_shared_chargers(vehicles: list[VehicleConfig]) -> None:
    counts: dict[str, int] = {}
    for v in vehicles:
        if v.enabled and v.entity("charger_energy"):
            counts[v.entity("charger_energy")] = counts.get(v.entity("charger_energy"), 0) + 1
    for v in vehicles:
        v.charger_shared = counts.get(v.entity("charger_energy"), 0) > 1


def parse_settings(options: dict, data_dir: str = "/data", store: dict | None = None) -> Settings:
    """Lukee yleiset asetukset (options) ja autot (store, eli /data/vehicles.json).

    Jos store puuttuu, autot luetaan v0.2:n options.vehicles-listasta.
    """
    s = Settings(data_dir=data_dir)
    for key in (
        "export_dir",
        "month_folder_format",
        "trip_start_distance_m",
        "trip_end_timeout_s",
        "ghost_motion_max_s",
        "home_zones",
        "stream_status_entity",
        "backfill_max_hours",
        "ha_mqtt_enabled",
        "report_xlsx",
        "report_csv",
        "xlsx_import_poll_s",
        "odometer_gap_tolerance_km",
        "auto_lock_after_days",
        "commute_counts_as_private",
        "archive_filtered",
        "nominatim_user_agent",
        "refuel_min_l",
        "log_level",
    ):
        if key in options and options[key] is not None:
            setattr(s, key, options[key])
    s.odometer_gap_tolerance_km = float(s.odometer_gap_tolerance_km)
    s.refuel_min_l = float(s.refuel_min_l)

    if store is None:
        store = migrate_options(options)
    s.home_charger_entity = str(store.get("home_charger_entity") or "").strip()

    seen: set[str] = set()
    for raw in store.get("vehicles") or []:
        v = vehicle_from_store(raw, s.home_charger_entity)
        errors = validate_vehicle(v)
        if v.id in seen:
            errors.append(f"Auton tunniste '{v.id}' on käytössä useammin kuin kerran.")
        key = v.id if v.id not in seen else f"{v.id}#{len(s.vehicles)}"
        seen.add(v.id)
        if errors:
            s.vehicle_errors[key] = errors
            for e in errors:
                _LOG.error("Auto %s: %s", v.id, e)
        if v.id and key == v.id:
            s.vehicles.append(v)
    mark_shared_chargers(s.vehicles)
    return s


def load_settings(path: str | None = None) -> Settings:
    from .vehicle_store import VehicleStore

    data_dir = os.environ.get("LOGBOOK_DATA_DIR", "/data")
    path = path or os.path.join(data_dir, "options.json")
    p = Path(path)
    options = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not p.exists():
        _LOG.warning("Asetustiedostoa %s ei löydy, käytetään oletuksia", path)
    from .transfer import import_package_if_fresh

    try:
        import_package_if_fresh(data_dir, options.get("export_dir") or Settings.export_dir)
    except Exception:
        _LOG.exception("Siirtopaketin tuonti epäonnistui")
    store = VehicleStore(data_dir).load_or_migrate(options)
    return parse_settings(options, data_dir=data_dir, store=store)
