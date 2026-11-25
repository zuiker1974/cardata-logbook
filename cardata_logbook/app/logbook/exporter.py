"""Tiedostojen kirjoitus /share/ajopaivakirja/-kansioon (Rclone Backup vie sen Driveen).

Kaikki kirjoitukset ovat atomisia: ensin <nimi>.tmp, sitten os.replace, jotta Rclone ei
koskaan siirrä puolivalmista tiedostoa (Rclone-job ohittaa *.tmp-tiedostot).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .vehicle_registry import Settings, VehicleConfig

_LOG = logging.getLogger(__name__)

SUBDIRS = ("arkisto", "varmuuskopiot", "tuonti", "tuonnit", "tuonnit/hylatyt")
BACKUP_KEEP_MONTHS = 12


def atomic_write(path: str | Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def unique_path(path: Path) -> Path:
    """Arkistotiedostoja ei koskaan ylikirjoiteta: palauttaa vapaan nimen (_v2, _v3 ...)."""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}_v{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


class Exporter:
    def __init__(self, settings: Settings, vehicle: VehicleConfig):
        self.settings = settings
        self.vehicle = vehicle
        self.root = Path(settings.export_dir)

    @property
    def car_dir(self) -> Path:
        return self.root / self.vehicle.folder_name

    def ensure_dirs(self) -> None:
        for sub in SUBDIRS:
            (self.car_dir / sub).mkdir(parents=True, exist_ok=True)

    def year_dir(self, year: int) -> Path:
        return self.car_dir / str(year)

    def month_dir(self, year: int, month: int) -> Path:
        name = datetime(year, month, 1).strftime(self.settings.month_folder_format or "%m")
        return self.year_dir(year) / name

    def archive_dir(self, year: int) -> Path:
        return self.car_dir / "arkisto" / str(year)

    def write(self, path: Path, data: bytes) -> Path:
        p = atomic_write(path, data)
        _LOG.info("Kirjoitettu %s", p)
        return p

    def write_archive(self, name: str, data: bytes, year: int) -> Path:
        target = unique_path(self.archive_dir(year) / name)
        return self.write(target, data)

    # ------------------------------------------------------------ varmuuskopio
    def backup_path(self, now: datetime) -> Path:
        return self.car_dir / "varmuuskopiot" / f"bmw_logbook_{self.vehicle.id}_backup_{now:%Y-%m}.db"

    def cleanup_backups(self, now: datetime) -> list[Path]:
        removed = []
        folder = self.car_dir / "varmuuskopiot"
        if not folder.exists():
            return removed
        limit = now.year * 12 + now.month - BACKUP_KEEP_MONTHS
        for p in folder.glob(f"bmw_logbook_{self.vehicle.id}_backup_*.db"):
            try:
                y, m = p.stem.rsplit("_", 1)[1].split("-")
                if int(y) * 12 + int(m) <= limit:
                    p.unlink()
                    removed.append(p)
            except (ValueError, IndexError):
                continue
        return removed

    # ---------------------------------------------------------------- tuonnit
    def car_dirs(self) -> list[Path]:
        """Kaikki tämän auton kansiot (rekisteritunnuksen vaihtuessa vanha kansio säilyy)."""
        if not self.root.exists():
            return []
        out = []
        for p in self.root.iterdir():
            if p.is_dir() and (p.name == self.vehicle.id or p.name.startswith(self.vehicle.id + "_")):
                # varmistetaan, ettei esim. "ix3" osu kansioon "ix3x_..."
                rest = p.name[len(self.vehicle.id):]
                if rest == "" or rest.startswith("_"):
                    out.append(p)
        return out

    def pending_imports(self, min_age_s: float = 5.0) -> list[Path]:
        files = []
        now = time.time()
        for d in self.car_dirs():
            folder = d / "tuonti"
            if not folder.exists():
                continue
            for p in sorted(folder.iterdir()):
                if p.is_file() and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$"):
                    if now - p.stat().st_mtime >= min_age_s:
                        files.append(p)
        return files

    def move_processed(self, path: Path, rejected: bool = False) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = path.parent.parent / ("tuonnit/hylatyt" if rejected else "tuonnit")
        folder.mkdir(parents=True, exist_ok=True)
        target = unique_path(folder / f"{stamp}_{path.name}")
        os.replace(path, target)
        return target

    def recent_files(self, limit: int = 15) -> list[dict]:
        if not self.car_dir.exists():
            return []
        files = []
        for p in self.car_dir.rglob("*"):
            if p.is_file() and not p.name.endswith(".tmp") and "tuonti" not in p.parts:
                st = p.stat()
                files.append({"path": str(p.relative_to(self.root)), "mtime": st.st_mtime, "size": st.st_size})
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return files[:limit]
