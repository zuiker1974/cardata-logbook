"""Autojen tallennus lisäosan omaan tiedostoon /data/vehicles.json.

Autot hallitaan ajopäiväkirjan Autot-sivulla. v0.2:ssa autot olivat lisäosan
asetussivulla (options.vehicles); ensimmäisellä käynnistyksellä ne siirretään tänne
sellaisinaan, minkä jälkeen options.vehicles-listaa ei enää lueta.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .vehicle_registry import STORE_VERSION, migrate_options

_LOG = logging.getLogger(__name__)

FILENAME = "vehicles.json"


class VehicleStore:
    def __init__(self, data_dir: str | os.PathLike):
        self.path = Path(data_dir) / FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("version", STORE_VERSION)
        data.setdefault("home_charger_entity", "")
        data.setdefault("vehicles", [])
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {**data, "version": STORE_VERSION}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def load_or_migrate(self, options: dict) -> dict:
        data = self.load()
        if data is not None:
            return data
        data = migrate_options(options)
        if data["vehicles"]:
            self.save(data)
            _LOG.info(
                "Autot siirretty lisäosan asetuksista tiedostoon %s: %s",
                self.path,
                ", ".join(v["id"] for v in data["vehicles"]),
            )
        return data
