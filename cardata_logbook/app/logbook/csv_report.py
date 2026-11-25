"""CSV-vienti (valinnainen). Sama sisältö kuin raporttien ajotaulukossa + auton tunniste ja rekisterinumero."""
from __future__ import annotations

import csv
import io

from . import timeutil
from .report_data import ReportContext, ReportFilter

CSV_HEADERS = [
    "vehicle_id",
    "rekisterinumero",
    "matka_id",
    "paivamaara",
    "alku",
    "loppu",
    "lahtopaikka",
    "paatepaikka",
    "mittari_alussa",
    "mittari_lopussa",
    "km",
    "ajotyyppi",
    "tarkoitus",
    "lataustauot",
    "merkinnat",
    "kommentti",
]


def build_csv(ctx: ReportContext, year: int, flt: ReportFilter | None = None) -> bytes:
    start, end = timeutil.year_bounds(year)
    rows = ctx.rows(ctx.trips(start, end, flt))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_HEADERS)
    for r in rows:
        plate = ctx.info_for(r["start_time"]).get("plate") or ctx.vehicle.plate
        w.writerow(
            [
                ctx.vehicle.id,
                plate,
                r["id"],
                r["date"],
                r["start"],
                r["end"],
                r["from"],
                r["to"],
                r["odo_start"],
                r["odo_end"],
                r["km"],
                r["type_label"],
                r["purpose"],
                r["charging"],
                ", ".join(r["markers"]),
                r["comment"],
            ]
        )
    return ("﻿" + buf.getvalue()).encode("utf-8")
