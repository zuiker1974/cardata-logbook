"""Siirtopaketti: lisäosan tietojen siirto toiseen asennukseen (esim. uuteen lisäosarepoon).

Toisesta reposta asennettu lisäosa on Home Assistantille eri lisäosa, eikä se näe vanhan
lisäosan /data-kansiota. Siksi vanha lisäosa kirjoittaa siirtopaketin jaettuun kansioon
(<raporttikansio>/.siirto/), ja uusi lisäosa tuo sen ensimmäisellä käynnistyksellä, jos sen
oma /data on vielä tyhjä.

Paketissa ovat autot (vehicles.json), autokohtaiset tietokannat ja tilakoneiden tilat sekä
osoitevälimuisti. Lisäosan asetussivun asetuksia (options.json) ei siirretä.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)

DIRNAME = ".siirto"
FILENAME = "ajopaivakirja_siirto.zip"
SKIP_NAMES = {"options.json"}


def package_path(export_dir: str | os.PathLike) -> Path:
    return Path(export_dir) / DIRNAME / FILENAME


def _sqlite_copy(src: Path, dest: Path) -> None:
    """Johdonmukainen kopio käytössä olevasta tietokannasta."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dest))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def export_package(data_dir: str | os.PathLike, export_dir: str | os.PathLike, services: dict | None = None) -> Path:
    data = Path(data_dir)
    target = package_path(export_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    for svc in (services or {}).values():
        # tilakoneiden viimeisin tila levylle ennen pakkausta
        try:
            if getattr(svc, "trip_manager", None) is not None:
                svc.trip_manager.save()
            if getattr(svc, "consumption", None) is not None:
                svc.consumption.save()
        except Exception:
            _LOG.exception("Tilan tallennus ennen siirtopakettia epäonnistui")
    tmp_zip = target.with_name(target.name + ".tmp")
    scratch = target.parent / "tmp_db"
    scratch.mkdir(exist_ok=True)
    count = 0
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(data.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(data).as_posix()
                if path.name in SKIP_NAMES or path.name.endswith((".tmp", "-wal", "-shm", "-journal")):
                    continue
                if path.suffix == ".db":
                    copy = scratch / (rel.replace("/", "__"))
                    _sqlite_copy(path, copy)
                    zf.write(copy, rel)
                    copy.unlink()
                else:
                    zf.write(path, rel)
                count += 1
            zf.writestr("SIIRTO.txt", f"Ajopäiväkirjan siirtopaketti {datetime.now().isoformat(timespec='seconds')}, {count} tiedostoa\n")
        os.replace(tmp_zip, target)
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink()
        for p in scratch.glob("*"):
            p.unlink()
        scratch.rmdir()
    _LOG.info("Siirtopaketti kirjoitettu: %s (%s tiedostoa)", target, count)
    return target


def data_is_empty(data_dir: str | os.PathLike) -> bool:
    data = Path(data_dir)
    if (data / "vehicles.json").exists():
        return False
    return not any(data.glob("*/logbook.db"))


def import_package_if_fresh(data_dir: str | os.PathLike, export_dir: str | os.PathLike) -> bool:
    """Tuo siirtopaketin, jos tämän asennuksen /data on tyhjä. Palauttaa True, jos tuotiin."""
    src = package_path(export_dir)
    if not src.exists() or not data_is_empty(data_dir):
        return False
    data = Path(data_dir).resolve()
    with zipfile.ZipFile(src) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/") and n != "SIIRTO.txt"]
        for name in names:
            dest = (data / name).resolve()
            if data not in dest.parents or Path(name).name in SKIP_NAMES:
                raise ValueError(f"Siirtopaketissa on kelvoton polku: {name}")
        for name in names:
            dest = data / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            with zf.open(name) as fsrc, open(tmp, "wb") as fdst:
                fdst.write(fsrc.read())
            os.replace(tmp, dest)
    done = src.with_name(f"tuotu_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{src.name}")
    src.rename(done)
    _LOG.warning("Siirtopaketti tuotu (%s tiedostoa). Paketti nimettiin uudelleen: %s", len(names), done.name)
    return True
