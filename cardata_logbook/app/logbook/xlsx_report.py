"""Excel-vuosityökirja (openpyxl) ja kommenttien tuonti takaisin Matka-ID:n perusteella."""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import timeutil
from .database import TRIP_TYPE_BY_LABEL, TRIP_TYPE_LABELS, LogbookError, VehicleDB
from .report_data import APP_VERSION, MONTH_NAMES, ReportContext, ReportFilter, consumption_row

FORMAT_VERSION = "1"
HEADERS = [
    "Päivämäärä",
    "Alku",
    "Loppu",
    "Lähtöpaikka",
    "Päätepaikka",
    "Mittari alussa",
    "Mittari lopussa",
    "km",
    "Ajotyyppi",
    "Tarkoitus",
    "Lataustauot / merkinnät",
    "Kommentti",
    "Matka-ID",
]
COL = {h: i + 1 for i, h in enumerate(HEADERS)}
WIDTHS = [11, 7, 7, 34, 34, 12, 12, 8, 13, 34, 30, 40, 9]
HEADER_ROW = 3
FIRST_ROW = 4
MAX_ROW = 2000
EDITABLE = ("Ajotyyppi", "Tarkoitus", "Kommentti")
TYPE_SHEETS = (("business", "Työajot"), ("private", "Yksityisajot"), ("commute", "Työmatkat"))
BACKUP_NOTE = (
    "Tämä on varmuuskopio. Tallenna kommentoitu versio auton tuonti-kansioon tai tuo se ajopäiväkirjan "
    "selainnäkymässä (Raportit → Tuo Excel). Drivessa olevaa tiedostoa ei kannata muokata paikallaan, "
    "koska seuraava varmuuskopio ylikirjoittaa sen."
)

CONS_HEADERS = [
    "Päivämäärä",
    "Aika",
    "Tapahtuma",
    "Paikka",
    "Määrä",
    "Yksikkö",
    "Varaus / tankki",
    "Lähde",
    "Yksikköhinta",
    "Hinnan yksikkö",
    "Kommentti",
    "Kulu-ID",
]
CONS_WIDTHS = [11, 12, 10, 36, 9, 8, 16, 20, 12, 10, 36, 8]
CONS_EDITABLE = ("Yksikköhinta", "Kommentti")
CONS_SHEET = "Kulut"
CONS_META = "_meta_kulut"

BOLD = Font(bold=True)
HEAD_FILL = PatternFill("solid", fgColor="E2E8F0")
EDIT_FILL = PatternFill("solid", fgColor="FEF9C3")
TITLE_FONT = Font(bold=True, size=13)


def _write_headers(ws) -> None:
    for i, h in enumerate(HEADERS, start=1):
        c = ws.cell(row=HEADER_ROW, column=i, value=h)
        c.font = BOLD
        c.fill = EDIT_FILL if h in EDITABLE else HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(i)].width = WIDTHS[i - 1]
    ws.column_dimensions[get_column_letter(COL["Matka-ID"])].hidden = True
    ws.freeze_panes = ws.cell(row=FIRST_ROW, column=1)


def _row_values(r: dict) -> list:
    notes = [r["charging"]] if r["charging"] else []
    if r["markers"]:
        notes.append("[" + ", ".join(r["markers"]) + "]")
    return [
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
        "; ".join(notes),
        r["comment"],
        r["id"],
    ]


def _protect(ws) -> None:
    # Suojaus ilman salasanaa: käyttäjä voi tarvittaessa poistaa sen, mutta lukitut sarakkeet ohitetaan tuonnissa
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False
    ws.protection.autoFilter = False
    ws.protection.sort = False


def build_xlsx(ctx: ReportContext, year: int, flt: ReportFilter | None = None) -> bytes:
    flt = flt or ReportFilter()
    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = "Yhteenveto"
    info = ctx.info_for(timeutil.year_bounds(year)[0])
    ys, ye = timeutil.year_bounds(year)
    meta_rows: list[list] = []
    locked_months = {m for m in range(1, 13) if ctx.db.is_locked(year, m)}

    for m in range(1, 13):
        ws = wb.create_sheet(f"{m:02d}")
        ws.cell(row=1, column=1, value=f"{MONTH_NAMES[m]} {year} – {ctx.vehicle.id} {ctx.vehicle.plate}").font = TITLE_FONT
        note = "Muokattavat sarakkeet: Ajotyyppi, Tarkoitus ja Kommentti (keltainen otsikko)."
        if m in locked_months:
            note += " Jakso on lukittu: vain kommentin voi lisätä (jälkikommentti)."
        ws.cell(row=2, column=1, value=note)
        _write_headers(ws)
        dv = DataValidation(type="list", formula1='"Työajo,Yksityisajo,Työmatka"', allow_blank=False)
        dv.error = "Valitse Työajo, Yksityisajo tai Työmatka"
        ws.add_data_validation(dv)
        ms, me = timeutil.month_bounds(year, m)
        rows = ctx.rows(ctx.trips(ms, me, flt))
        for i, r in enumerate(rows):
            row = FIRST_ROW + i
            for col, value in enumerate(_row_values(r), start=1):
                c = ws.cell(row=row, column=col, value=value)
                c.alignment = Alignment(wrap_text=col in (4, 5, 10, 11, 12), vertical="top")
                header = HEADERS[col - 1]
                editable = header == "Kommentti" or (header in EDITABLE and m not in locked_months)
                if editable:
                    c.protection = Protection(locked=False)
            dv.add(ws.cell(row=row, column=COL["Ajotyyppi"]))
            meta_rows.append([r["id"], r["type"], r["purpose"], r["comment"], r["odo_start"], r["odo_end"], r["from"], r["to"], r["date"], int(m in locked_months)])
        _protect(ws)

    # Yhteenveto
    ws = summary_ws
    ws.cell(row=1, column=1, value=f"Ajopäiväkirja {year} – {ctx.vehicle.id}").font = TITLE_FONT
    if flt.is_partial:
        ws.cell(row=2, column=1, value=flt.title).font = Font(bold=True, color="1D4ED8")
    facts = [
        ("Auto", f"{info.get('make') or ctx.vehicle.make} {info.get('model') or ctx.vehicle.model}"),
        ("Auton tunniste", ctx.vehicle.id),
        ("Rekisterinumero", info.get("plate") or ctx.vehicle.plate),
        ("VIN", info.get("vin") or ctx.vehicle.vin),
        ("Voimalinja", info.get("powertrain") or ctx.vehicle.powertrain),
        ("Kuljettaja", info.get("driver") or ctx.vehicle.driver),
        ("Luotu", timeutil.fmt_datetime(timeutil.now_iso())),
    ]
    for i, (k, v) in enumerate(facts, start=3):
        ws.cell(row=i, column=1, value=k).font = BOLD
        ws.cell(row=i, column=2, value=v)
    ws.cell(row=11, column=1, value=BACKUP_NOTE).alignment = Alignment(wrap_text=True)
    ws.merge_cells(start_row=11, start_column=1, end_row=11, end_column=7)
    ws.row_dimensions[11].height = 45
    head = ["Kuukausi", "Alkulukema", "Loppulukema", "Työajot km", "Yksityisajot km (sis. työmatkat)", "josta työmatkat km", "Yhteensä km"]
    hr = 13
    for i, h in enumerate(head, start=1):
        c = ws.cell(row=hr, column=i, value=h)
        c.font = BOLD
        c.fill = HEAD_FILL
        c.alignment = Alignment(wrap_text=True)
    for i, w in enumerate([14, 13, 13, 12, 16, 14, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    rng = lambda col: f"'{{s}}'!{col}{FIRST_ROW}:{col}{MAX_ROW}"  # noqa: E731
    for m in range(1, 13):
        r = hr + m
        s = f"{m:02d}"
        F, G, H, I = (rng(c).format(s=s) for c in ("F", "G", "H", "I"))
        ws.cell(row=r, column=1, value=MONTH_NAMES[m])
        ws.cell(row=r, column=2, value=f'=IF(COUNT({F})=0,"",MIN({F}))')
        ws.cell(row=r, column=3, value=f'=IF(COUNT({G})=0,"",MAX({G}))')
        ws.cell(row=r, column=4, value=f'=SUMIF({I},"Työajo",{H})')
        ws.cell(row=r, column=5, value=f'=SUMIF({I},"Yksityisajo",{H})+SUMIF({I},"Työmatka",{H})')
        ws.cell(row=r, column=6, value=f'=SUMIF({I},"Työmatka",{H})')
        ws.cell(row=r, column=7, value=f"=SUM({H})")
    tr = hr + 13
    ws.cell(row=tr, column=1, value="Yhteensä").font = BOLD
    ws.cell(row=tr, column=2, value=f'=IF(COUNT(B{hr+1}:B{hr+12})=0,"",MIN(B{hr+1}:B{hr+12}))').font = BOLD
    ws.cell(row=tr, column=3, value=f'=IF(COUNT(C{hr+1}:C{hr+12})=0,"",MAX(C{hr+1}:C{hr+12}))').font = BOLD
    for col in "DEFG":
        ws[f"{col}{tr}"] = f"=SUM({col}{hr+1}:{col}{hr+12})"
        ws[f"{col}{tr}"].font = BOLD
    ws.cell(row=tr + 1, column=1, value="Työajo-%")
    ws.cell(row=tr + 1, column=4, value=f'=IF(G{tr}=0,"",D{tr}/G{tr})').number_format = "0.0 %"
    ws.cell(row=tr + 2, column=1, value="Yksityisajo-%")
    ws.cell(row=tr + 2, column=5, value=f'=IF(G{tr}=0,"",E{tr}/G{tr})').number_format = "0.0 %"
    _protect(ws)

    # Ajotyyppikohtaiset välilehdet (vain luku), vain koko vuoden työkirjaan
    if not flt.is_partial:
        for ttype, title in TYPE_SHEETS:
            trips = ctx.db.list_trips(ys, ye, trip_types=[ttype])
            if ttype == "commute" and not trips:
                continue
            tws = wb.create_sheet(title)
            tws.cell(row=1, column=1, value=f"{title} {year} – {ctx.vehicle.id} {ctx.vehicle.plate}").font = TITLE_FONT
            tws.cell(row=2, column=1, value="Kommentit kirjoitetaan kuukausivälilehdille. Tämä välilehti on vain luku.").font = Font(italic=True)
            _write_headers(tws)
            row = FIRST_ROW
            year_total = 0.0
            for m in range(1, 13):
                ms, me = timeutil.month_bounds(year, m)
                mrows = ctx.rows([t for t in trips if ms <= t["start_time"] < me])
                if not mrows:
                    continue
                subtotal = 0.0
                for r in mrows:
                    for col, value in enumerate(_row_values(r), start=1):
                        tws.cell(row=row, column=col, value=value).alignment = Alignment(wrap_text=col in (4, 5, 10, 11, 12), vertical="top")
                    subtotal += r["km"] or 0
                    row += 1
                tws.cell(row=row, column=1, value=f"{MONTH_NAMES[m]} yhteensä").font = BOLD
                tws.cell(row=row, column=8, value=round(subtotal, 1)).font = BOLD
                year_total += subtotal
                row += 2
            tws.cell(row=row, column=1, value=f"Vuosi {year} yhteensä").font = BOLD
            tws.cell(row=row, column=8, value=round(year_total, 1)).font = BOLD
            _protect(tws)

    if not flt.is_partial:
        _consumption_sheets(wb, ctx, year, locked_months)

    meta = wb.create_sheet("_meta")
    kv = [
        ("format_version", FORMAT_VERSION),
        ("app_version", APP_VERSION),
        ("vehicle_id", ctx.vehicle.id),
        ("vin", info.get("vin") or ctx.vehicle.vin or ""),
        ("plate", info.get("plate") or ctx.vehicle.plate or ""),
        ("year", year),
        ("created_at", timeutil.now_iso()),
        ("filter", flt.key),
        ("include_commute", int(flt.include_commute)),
    ]
    for i, (k, v) in enumerate(kv, start=1):
        meta.cell(row=i, column=1, value=k)
        meta.cell(row=i, column=2, value=v)
    start = len(kv) + 2
    for i, h in enumerate(["trip_id", "trip_type", "purpose", "comment", "odo_start", "odo_end", "from", "to", "date", "locked"], start=1):
        meta.cell(row=start, column=i, value=h)
    for j, mr in enumerate(meta_rows, start=start + 1):
        for i, v in enumerate(mr, start=1):
            meta.cell(row=j, column=i, value=v)
    meta.sheet_state = "hidden"
    _protect(meta)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _consumption_sheets(wb, ctx: ReportContext, year: int, locked_months: set) -> None:
    ys, ye = timeutil.year_bounds(year)
    rows = [consumption_row(e) for e in ctx.db.list_consumption(ys, ye)]
    ws = wb.create_sheet(CONS_SHEET)
    ws.cell(row=1, column=1, value=f"Kulut {year} – {ctx.vehicle.id} {ctx.vehicle.plate} (määrät)").font = TITLE_FONT
    ws.cell(
        row=2,
        column=1,
        value="Muokattavat sarakkeet: Yksikköhinta (vapaaehtoinen, esim. 0,12 tai 1,89) ja Kommentti (keltainen otsikko). "
        "Euromääriä ei lasketa.",
    )
    for i, h in enumerate(CONS_HEADERS, start=1):
        c = ws.cell(row=HEADER_ROW, column=i, value=h)
        c.font = BOLD
        c.fill = EDIT_FILL if h in CONS_EDITABLE else HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(i)].width = CONS_WIDTHS[i - 1]
    ws.column_dimensions[get_column_letter(len(CONS_HEADERS))].hidden = True
    ws.freeze_panes = ws.cell(row=FIRST_ROW, column=1)
    meta = wb.create_sheet(CONS_META)
    meta.append(["event_id", "unit_price", "comment"])
    for i, r in enumerate(rows):
        row = FIRST_ROW + i
        t = r["start"] + (f"–{r['end']}" if r["end"] and r["end"] != r["start"] else "")
        values = [
            r["date"],
            t,
            r["kind_label"],
            r["place"],
            r["quantity"],
            r["unit"],
            r["level"],
            r["source_label"] + (f" ({'; '.join(r['markers'])})" if r["markers"] else ""),
            r["unit_price"],
            r["unit_price_label"],
            r["comment"],
            r["id"],
        ]
        for col, value in enumerate(values, start=1):
            c = ws.cell(row=row, column=col, value=value)
            c.alignment = Alignment(wrap_text=col in (4, 8, 11), vertical="top")
            if CONS_HEADERS[col - 1] in CONS_EDITABLE:
                c.protection = Protection(locked=False)
            if CONS_HEADERS[col - 1] == "Yksikköhinta":
                c.number_format = "0.00##"
        meta.append([r["id"], r["unit_price"], r["comment"]])
    if not rows:
        ws.cell(row=FIRST_ROW, column=1, value="Ei latauksia tai tankkauksia tänä vuonna.")
    # kuukausittaiset määrät
    sr = FIRST_ROW + max(len(rows), 1) + 2
    ws.cell(row=sr, column=1, value="Kuukausittain").font = BOLD
    head = ["Kuukausi", "Ladattu kWh", "josta arvio kWh"] + (["Tankattu l", "Kulunut l (tankin lukema)"] if ctx.vehicle.has_fuel else [])
    for i, h in enumerate(head, start=1):
        c = ws.cell(row=sr + 1, column=i, value=h)
        c.font = BOLD
        c.fill = HEAD_FILL
        c.alignment = Alignment(wrap_text=True)
    for m in range(1, 13):
        ms, me = timeutil.month_bounds(year, m)
        c = ctx.consumption_summary(ms, me)
        vals = [MONTH_NAMES[m], c["charged_kwh"], c["charged_estimated_kwh"]]
        if ctx.vehicle.has_fuel:
            vals += [c["refuelled_l"], c["fuel_used_l"]]
        for i, v in enumerate(vals, start=1):
            ws.cell(row=sr + 1 + m, column=i, value=v)
    tr = sr + 14
    ws.cell(row=tr, column=1, value="Yhteensä").font = BOLD
    for i in range(2, len(head) + 1):
        col = get_column_letter(i)
        ws.cell(row=tr, column=i, value=f"=SUM({col}{sr + 2}:{col}{sr + 13})").font = BOLD
    _protect(ws)
    meta.sheet_state = "hidden"
    _protect(meta)


def _import_consumption(db: VehicleDB, wb, res: "ImportResult", source: str, filename: str) -> None:
    if CONS_SHEET not in wb.sheetnames:
        return
    exported: dict[int, dict] = {}
    if CONS_META in wb.sheetnames:
        for row in wb[CONS_META].iter_rows(min_row=2, values_only=True):
            if row and row[0] is not None:
                try:
                    exported[int(row[0])] = {"unit_price": row[1], "comment": _norm(row[2])}
                except (TypeError, ValueError):
                    continue
    ws = wb[CONS_SHEET]
    cols: dict[str, int] = {}
    header_row = None
    for r in range(1, 10):
        values = [ws.cell(row=r, column=c).value for c in range(1, len(CONS_HEADERS) + 3)]
        if "Kulu-ID" in values:
            header_row = r
            cols = {str(v): i + 1 for i, v in enumerate(values) if v}
            break
    if header_row is None:
        return
    for r in range(header_row + 1, ws.max_row + 1):
        raw_id = ws.cell(row=r, column=cols["Kulu-ID"]).value
        if raw_id in (None, ""):
            continue
        try:
            eid = int(_num(raw_id))
        except (TypeError, ValueError):
            continue
        ev = db.get_consumption(eid)
        exp = exported.get(eid)
        if ev is None or exp is None:
            res.unknown_rows.append(f"Kulut rivi {r}: Kulu-ID {eid} ei kuulu tämän auton kirjanpitoon")
            continue
        get = lambda h: ws.cell(row=r, column=cols[h]).value if h in cols else None  # noqa: E731
        # yksikköhinta
        xv = get("Yksikköhinta")
        xnum = _num(xv)
        if isinstance(xnum, str):
            res.skipped.append(f"Kulu {eid}: yksikköhinta '{xv}' ei ole luku")
        elif _num(exp["unit_price"]) != xnum:
            if _num(ev["unit_price"]) not in (_num(exp["unit_price"]), xnum):
                res.conflicts.append(f"Kulu {eid}: yksikköhinta muuttunut sekä selaimessa että Excelissä, Excelin arvoa ei tuotu")
            else:
                try:
                    db.update_consumption(eid, "unit_price", xnum, source=source, reason=f"Excel-tuonti {filename}")
                    res.applied.append(f"Kulu {eid}: yksikköhinta päivitetty")
                    res.changed_consumption_ids.add(eid)
                except LogbookError as exc:
                    res.skipped.append(f"Kulu {eid}: {exc}")
        # kommentti
        xc = _norm(get("Kommentti"))
        if xc != exp["comment"]:
            current = ev["comment"] or ""
            new = xc
            if current != exp["comment"] and current != xc:
                new = " | ".join(x for x in (current, xc) if x)
                res.conflicts.append(f"Kulu {eid}: kommentti muuttunut sekä selaimessa että Excelissä, tekstit yhdistetty")
            if new != current:
                db.update_consumption(eid, "comment", new, source=source, reason=f"Excel-tuonti {filename}")
                res.applied.append(f"Kulu {eid}: kommentti päivitetty")
                res.changed_consumption_ids.add(eid)


# ---------------------------------------------------------------------- tuonti
class ImportRejected(LogbookError):
    pass


@dataclass
class ImportResult:
    filename: str
    source: str
    applied: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unknown_rows: list[str] = field(default_factory=list)
    locked_changes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    changed_trip_ids: set = field(default_factory=set)
    changed_consumption_ids: set = field(default_factory=set)

    @property
    def changed_rows(self) -> int:
        return len(self.changed_trip_ids)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "conflicts": self.conflicts,
            "unknown_rows": self.unknown_rows,
            "locked_changes": self.locked_changes,
            "skipped": self.skipped,
            "changed_rows": self.changed_rows,
        }

    def status_text(self) -> str:
        parts = [f"{self.changed_rows} matkaa päivitetty"]
        if self.changed_consumption_ids:
            parts.append(f"{len(self.changed_consumption_ids)} kulua päivitetty")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} ristiriitaa")
        if self.unknown_rows:
            parts.append(f"{len(self.unknown_rows)} tuntematonta riviä")
        if self.locked_changes:
            parts.append("Excelissä muutettu lukittua tietoa, ei tuotu")
        return ", ".join(parts)


def _norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        return v


def read_meta(wb) -> tuple[dict, dict[int, dict]]:
    if "_meta" not in wb.sheetnames:
        raise ImportRejected("Tiedosto ei ole ajopäiväkirjan Excel (_meta-välilehti puuttuu).")
    ws = wb["_meta"]
    kv: dict = {}
    rows: dict[int, dict] = {}
    header = None
    for row in ws.iter_rows(values_only=True):
        if not row or row[0] is None:
            continue
        if header is None:
            if row[0] == "trip_id":
                header = list(row)
                continue
            kv[str(row[0])] = row[1]
        else:
            rec = dict(zip(header, row))
            try:
                rows[int(rec["trip_id"])] = rec
            except (TypeError, ValueError):
                continue
    return kv, rows


def import_xlsx(
    db: VehicleDB,
    vehicle_id: str,
    vin: str,
    data: bytes,
    filename: str,
    source: str = "excel_upload",
) -> ImportResult:
    """Tuo Excelissä muuttuneet kommentit (ja avoimen jakson ajotyypit/tarkoitukset).

    Hylkää toisen auton tiedoston. Lukitut mittaussarakkeet ohitetaan aina.
    """
    if db.vehicle_id != vehicle_id:
        raise ImportRejected("Väärä tietokanta tälle autolle.")
    try:
        wb = load_workbook(io.BytesIO(data), data_only=False)
    except Exception as exc:
        raise ImportRejected(f"Tiedostoa ei voitu lukea Excelinä: {exc}") from exc
    kv, meta_rows = read_meta(wb)
    file_vehicle = _norm(kv.get("vehicle_id"))
    if file_vehicle != vehicle_id:
        raise ImportRejected(
            f"Tiedosto kuuluu autolle '{file_vehicle}', ei autolle '{vehicle_id}'. Toisen auton Exceliä ei voi tuoda."
        )
    file_vin = _norm(kv.get("vin")).upper()
    if file_vin and vin and file_vin != vin.upper():
        raise ImportRejected(f"Tiedoston VIN {file_vin} ei vastaa auton VIN-tunnusta {vin}.")

    res = ImportResult(filename=filename, source=source)
    seen_ids: set[int] = set()
    for m in range(1, 13):
        name = f"{m:02d}"
        if name not in wb.sheetnames:
            continue
        ws = wb[name]
        header_row = None
        cols: dict[str, int] = {}
        for r in range(1, 10):
            values = [ws.cell(row=r, column=c).value for c in range(1, len(HEADERS) + 3)]
            if "Matka-ID" in values:
                header_row = r
                cols = {str(v): i + 1 for i, v in enumerate(values) if v}
                break
        if header_row is None:
            continue
        for r in range(header_row + 1, ws.max_row + 1):
            get = lambda h: ws.cell(row=r, column=cols[h]).value if h in cols else None  # noqa: E731
            raw_id = get("Matka-ID")
            values = [get(h) for h in HEADERS if h != "Matka-ID"]
            if raw_id in (None, ""):
                if any(v not in (None, "") for v in values):
                    res.unknown_rows.append(f"Välilehti {name} rivi {r}: ei Matka-ID:tä, riviä ei tuotu")
                continue
            try:
                tid = int(_num(raw_id))
            except (TypeError, ValueError):
                res.unknown_rows.append(f"Välilehti {name} rivi {r}: virheellinen Matka-ID {raw_id}")
                continue
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            trip = db.get_trip(tid)
            meta = meta_rows.get(tid)
            if trip is None or meta is None:
                res.unknown_rows.append(f"Välilehti {name} rivi {r}: Matka-ID {tid} ei kuulu tämän auton ajopäiväkirjaan")
                continue
            # lukitut sarakkeet: ohitetaan, mutta poikkeama raportoidaan
            for header, key, numeric in (
                ("Mittari alussa", "odo_start", True),
                ("Mittari lopussa", "odo_end", True),
                ("Lähtöpaikka", "from", False),
                ("Päätepaikka", "to", False),
                ("Päivämäärä", "date", False),
            ):
                if header not in cols:
                    continue
                xv, mv = get(header), meta.get(key)
                same = _num(xv) == _num(mv) if numeric else _norm(xv) == _norm(mv)
                if not same:
                    res.locked_changes.append(f"Matka {tid}: '{header}' muutettu Excelissä ({_norm(mv)} → {_norm(xv)}), ei tuotu")
            _import_trip(db, res, tid, trip, meta, get, source, filename)
    _import_consumption(db, wb, res, source, filename)
    status = "ok"
    if res.conflicts:
        status = "conflicts"
    db.log_import(source, filename, status, res.changed_rows, res.as_dict())
    return res


def _import_trip(db: VehicleDB, res: ImportResult, tid: int, trip: dict, meta: dict, get, source: str, filename: str) -> None:
    excel = {
        "comment": _norm(get("Kommentti")),
        "purpose": _norm(get("Tarkoitus")),
        "trip_type": _norm(get("Ajotyyppi")),
    }
    exported = {
        "comment": _norm(meta.get("comment")),
        "purpose": _norm(meta.get("purpose")),
        "trip_type": TRIP_TYPE_LABELS.get(_norm(meta.get("trip_type")), _norm(meta.get("trip_type"))),
    }
    target = trip
    redirected = False
    if trip["merged_into_id"] is not None:
        target = db.active_root(tid)
        redirected = True
    locked = db.trip_is_locked(target)
    for field_name in ("comment", "trip_type", "purpose"):
        xv, ev = excel[field_name], exported[field_name]
        if xv == ev:
            continue
        if field_name == "trip_type":
            code = TRIP_TYPE_BY_LABEL.get(xv.lower())
            if code is None:
                res.skipped.append(f"Matka {tid}: tuntematon ajotyyppi '{xv}'")
                continue
            new_value = code
        else:
            new_value = xv
        if field_name != "comment" and redirected:
            res.skipped.append(f"Matka {tid} on yhdistetty matkaan {target['id']}: {field_name} ei tuotu")
            continue
        if field_name != "comment" and locked:
            res.skipped.append(f"Matka {tid}: jakso on lukittu, {'ajotyyppi' if field_name == 'trip_type' else 'tarkoitus'} ei tuotu")
            continue
        current = target[field_name] or ""
        if field_name == "comment":
            if redirected:
                if xv and xv not in current:
                    new_value = " | ".join(x for x in (current, xv) if x)
                else:
                    continue
            elif current != exported["comment"] and current != xv:
                # muutettu myös käyttöliittymässä viennin jälkeen: yhdistetään molemmat
                new_value = " | ".join(x for x in (current, xv) if x)
                res.conflicts.append(f"Matka {tid}: kommentti muuttunut sekä selaimessa että Excelissä, tekstit yhdistetty")
            if new_value == current:
                continue
            db.update_field(target["id"], "comment", new_value, source=source, reason=f"Excel-tuonti {filename}")
            res.applied.append(f"Matka {target['id']}: kommentti päivitetty")
            res.changed_trip_ids.add(target["id"])
            continue
        current_cmp = current if field_name == "purpose" else current  # trip_type koodina
        exported_cmp = exported[field_name] if field_name == "purpose" else TRIP_TYPE_BY_LABEL.get(exported[field_name].lower(), exported[field_name])
        if current_cmp == new_value:
            continue
        if current_cmp != exported_cmp:
            db.add_conflict(target["id"], field_name, current_cmp, new_value, filename, source)
            res.conflicts.append(f"Matka {tid}: {field_name} muuttunut sekä selaimessa että Excelissä, valitse oikea arvo")
            continue
        db.update_field(target["id"], field_name, new_value, source=source, reason=f"Excel-tuonti {filename}")
        res.applied.append(f"Matka {tid}: {'ajotyyppi' if field_name == 'trip_type' else 'tarkoitus'} päivitetty")
        res.changed_trip_ids.add(target["id"])
