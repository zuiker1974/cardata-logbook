"""Autojen ja entiteettien automaattinen löytö HA:n CarData-integraatiosta.

Integraation (bmw-cardata-ha, domain ``cardata``) jokaisen entiteetin unique_id on muotoa
``<VIN>_<CarData-avain>``. Rooli päätellään CarData-avaimesta, joten entiteetin nimellä
(esim. ``_2``-pääte toisella autolla) ei ole väliä. Metadata-sensorista saadaan malli ja
käyttövoima. Lisäosa ei avaa omaa BMW-yhteyttä: kaikki luetaan HA:n rekisteristä ja tiloista.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PLATFORM = "cardata"
VIN_PREFIX_RE = re.compile(r"^([A-HJ-NPR-Z0-9]{17})_(.+)$")

# CarData-avain (unique_id:n VIN-osan jälkeen) -> ajopäiväkirjan rooli
KEY_ROLES = {
    "device_tracker": "location",
    "vehicle.vehicle.travelledDistance": "odometer",
    "vehicle.isMoving": "motion",
    "vehicle.powertrain.electric.battery.stateOfCharge.displayed": "soc",
    "vehicle.drivetrain.electricEngine.charging.status": "charging_state",
    "vehicle.drivetrain.electricEngine.charging.method": "charging_method",
    "vehicle.drivetrain.electricEngine.charging.hvStatus": "hv_system_state",
    "vehicle.drivetrain.fuelSystem.remainingFuel": "fuel_level",
}
METADATA_KEY = "diagnostics_vehicle_metadata"
STREAM_STATUS_SUFFIX = "_diagnostics_connection_status"

POWERTRAINS = ("BEV", "PHEV", "ICE")


@dataclass
class DiscoveredVehicle:
    vin: str
    entities: dict[str, str] = field(default_factory=dict)
    metadata_entity: str = ""
    brand: str = ""
    model: str = ""
    series: str = ""
    drive_train: str = ""

    @property
    def powertrain(self) -> str:
        return powertrain_from_drive_train(self.drive_train)

    @property
    def display_name(self) -> str:
        name = f"{self.brand} {self.model}".strip()
        if self.series and self.series not in name:
            name = f"{name} ({self.series})"
        return name or self.vin


def powertrain_from_drive_train(value: str | None) -> str:
    """BMW:n driveTrain-arvo -> BEV, PHEV tai ICE ('' jos tuntematon).

    Mild hybrid (MHEV) ja pelkkä polttomoottori käsitellään ICE:nä, koska niitä ei ladata.
    """
    v = (value or "").strip().upper()
    if not v:
        return ""
    if v in ("BEV", "ELECTRIC") or v.startswith("BEV"):
        return "BEV"
    if "PHEV" in v or v in ("PLUGIN_HYBRID", "PLUG_IN_HYBRID"):
        return "PHEV"
    return "ICE"


def _metadata(attrs: dict) -> dict:
    basic = attrs.get("vehicle_basic_data") or {}
    raw = attrs.get("vehicle_basic_data_raw") or {}
    return {
        "brand": str(raw.get("brand") or basic.get("brand") or "").strip(),
        "model": str(basic.get("model_name") or raw.get("modelName") or "").strip(),
        "series": str(basic.get("series_development") or raw.get("seriesDevt") or "").strip(),
        "drive_train": str(basic.get("drive_train") or raw.get("driveTrain") or "").strip(),
    }


def discover(registry: list[dict], states: dict[str, dict] | None = None) -> dict[str, DiscoveredVehicle]:
    """Palauttaa {VIN: DiscoveredVehicle} HA:n entiteettirekisteristä."""
    states = states or {}
    found: dict[str, DiscoveredVehicle] = {}
    for entry in registry or []:
        if entry.get("platform") != PLATFORM or entry.get("disabled_by"):
            continue
        m = VIN_PREFIX_RE.match(str(entry.get("unique_id") or ""))
        if not m:
            continue
        vin, key = m.group(1), m.group(2)
        eid = entry.get("entity_id") or ""
        v = found.setdefault(vin, DiscoveredVehicle(vin=vin))
        role = KEY_ROLES.get(key)
        if role and role not in v.entities:
            v.entities[role] = eid
        elif key == METADATA_KEY:
            v.metadata_entity = eid
            meta = _metadata((states.get(eid) or {}).get("attributes") or {})
            v.brand, v.model, v.series, v.drive_train = meta["brand"], meta["model"], meta["series"], meta["drive_train"]
    # VIN, josta ei löytynyt yhtään ajopäiväkirjan roolia, ei ole auto (esim. muu laite)
    return {vin: v for vin, v in found.items() if v.entities}


def find_stream_status(registry: list[dict]) -> str:
    for entry in registry or []:
        if entry.get("platform") == PLATFORM and str(entry.get("unique_id") or "").endswith(STREAM_STATUS_SUFFIX):
            return entry.get("entity_id") or ""
    return ""


def suggest_id(d: DiscoveredVehicle, taken: set[str]) -> str:
    """Ehdotettu auton tunniste mallin nimestä, esim. 'iX3' -> 'ix3', '530e xDrive' -> '530e'."""
    base = re.sub(r"[^a-z0-9]", "", (d.model.split(" ")[0] if d.model else "").lower())[:16]
    if len(base) < 2:
        base = "auto" + d.vin[-4:].lower()
    cand, n = base, 2
    while cand in taken:
        suffix = f"_{n}"
        cand = base[: 16 - len(suffix)] + suffix
        n += 1
    return cand


__all__ = [
    "DiscoveredVehicle",
    "KEY_ROLES",
    "POWERTRAINS",
    "discover",
    "find_stream_status",
    "powertrain_from_drive_train",
    "suggest_id",
]
