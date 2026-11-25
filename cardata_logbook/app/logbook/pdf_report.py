"""PDF-raportit (ReportLab). Jokainen raportti koskee täsmälleen yhtä autoa."""
from __future__ import annotations

import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import timeutil
from .database import CORRECTION_FIELDS
from .integrity import summarize
from .report_data import APP_VERSION, MONTH_NAMES, ReportContext, ReportFilter, fmt_km, fmt_qty

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"


def _register_fonts() -> None:
    global _FONT, _FONT_BOLD
    candidates = [
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf", "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("DejaVu", regular))
                pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
                _FONT, _FONT_BOLD = "DejaVu", "DejaVu-Bold"
            except Exception:  # pragma: no cover
                pass
            return


_register_fonts()

PAGE = landscape(A4)
STYLES = getSampleStyleSheet()
S_TITLE = ParagraphStyle("t", parent=STYLES["Title"], fontName=_FONT_BOLD, fontSize=16, spaceAfter=4)
S_H2 = ParagraphStyle("h2", parent=STYLES["Heading2"], fontName=_FONT_BOLD, fontSize=12, spaceBefore=8, spaceAfter=4)
S_BODY = ParagraphStyle("b", parent=STYLES["BodyText"], fontName=_FONT, fontSize=8.5, leading=10.5)
S_CELL = ParagraphStyle("c", parent=S_BODY, fontSize=7.2, leading=8.6)
S_CELL_R = ParagraphStyle("cr", parent=S_CELL, alignment=TA_RIGHT)
S_CELL_B = ParagraphStyle("cb", parent=S_CELL, fontName=_FONT_BOLD)
S_WARN = ParagraphStyle("w", parent=S_BODY, textColor=colors.HexColor("#9a3412"))
S_PARTIAL = ParagraphStyle("p", parent=S_BODY, fontName=_FONT_BOLD, fontSize=11, textColor=colors.HexColor("#1d4ed8"))


def P(text, style=S_CELL) -> Paragraph:
    return Paragraph(escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def _page_decorator(ctx: ReportContext, period_label: str, footer_note: str):
    plate = ctx.vehicle.plate or "(ei rekisteritunnusta)"
    created = timeutil.fmt_datetime(timeutil.now_iso())

    def draw(canvas, doc):
        canvas.saveState()
        w, h = PAGE
        canvas.setFont(_FONT_BOLD, 9)
        canvas.drawString(12 * mm, h - 10 * mm, f"CarData Logbook · {ctx.vehicle.id} · {plate}")
        canvas.setFont(_FONT, 9)
        canvas.drawRightString(w - 12 * mm, h - 10 * mm, f"{ctx.vehicle.display_name} · {period_label}")
        canvas.setStrokeColor(colors.HexColor("#94a3b8"))
        canvas.line(12 * mm, h - 12 * mm, w - 12 * mm, h - 12 * mm)
        canvas.setFont(_FONT, 6.5)
        canvas.drawString(12 * mm, 7 * mm, f"Luotu {created} · CarData Logbook v{APP_VERSION}" + (f" · {footer_note}" if footer_note else ""))
        canvas.drawRightString(w - 12 * mm, 7 * mm, f"Sivu {doc.page}")
        canvas.restoreState()

    return draw


def _info_table(ctx: ReportContext, when_iso: str | None, period_label: str, summary: dict, drivers: list[str]) -> Table:
    info = ctx.info_for(when_iso)
    left = [
        ("Auto", f"{info.get('make') or ctx.vehicle.make} {info.get('model') or ctx.vehicle.model}"),
        ("Voimalinja", info.get("powertrain") or ctx.vehicle.powertrain),
        ("Rekisterinumero", info.get("plate") or ctx.vehicle.plate or "–"),
        ("VIN", info.get("vin") or ctx.vehicle.vin or "–"),
        ("Kuljettaja", ", ".join(drivers) if drivers else (info.get("driver") or ctx.vehicle.driver or "–")),
        ("Raportointijakso", period_label),
    ]
    right = [
        ("Mittarilukema jakson alussa", fmt_km(summary.get("odometer_start")) or "–"),
        ("Mittarilukema jakson lopussa", fmt_km(summary.get("odometer_end")) or "–"),
        ("Ajettu yhteensä", f"{fmt_km(summary['total_km'])} km"),
        ("Työajot", f"{fmt_km(summary['business_km'])} km ({summary['business_pct']:.1f} %)"),
        (
            "Yksityisajot (sis. työmatkat)",
            f"{fmt_km(summary['private_all_km'])} km ({summary['private_all_pct']:.1f} %)",
        ),
        ("  josta työmatkat", f"{fmt_km(summary['commute_km'])} km"),
    ]
    data = [[P(a, S_CELL_B), P(b), P(c, S_CELL_B), P(d)] for (a, b), (c, d) in zip(left, right)]
    t = Table(data, colWidths=[38 * mm, 90 * mm, 52 * mm, 60 * mm])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f5f9")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return t


TRIP_HEADERS = [
    "Pvm",
    "Aika",
    "Lähtöpaikka",
    "Määränpää",
    "Mittari\nalussa",
    "Mittari\nlopussa",
    "km",
    "Ajotyyppi",
    "Tarkoitus / lisätiedot",
    "Kommentti (lisätieto)",
]
COL_W = [17, 18, 46, 46, 17, 17, 12, 18, 50, 32]


def _trip_table(rows: list[dict], subtotal_label: str | None = None, show_driver: bool = False) -> Table:
    header = [P(h, S_CELL_B) for h in TRIP_HEADERS]
    data = [header]
    total = 0.0
    for r in rows:
        extra = []
        if r["charging"]:
            extra.append(r["charging"])
        if r["markers"]:
            extra.append("[" + ", ".join(r["markers"]) + "]")
        purpose = r["purpose"] or ("(tarkoitus puuttuu)" if r["type"] == "business" else "")
        purpose_text = "\n".join([purpose] + extra) if purpose else "\n".join(extra)
        comment = r["comment"]
        if comment and r["comment_post_lock"]:
            comment += "\n(lisätty lukituksen jälkeen)"
        time_text = f"{r['start']}–{r['end']}"
        if r["end_date"] != r["date"]:
            time_text += f"\n(päättyi {r['end_date'][:6]})"
        total += r["km"] or 0
        data.append(
            [
                P(r["date"]),
                P(time_text),
                P(r["from"]),
                P(r["to"]),
                P(fmt_km(r["odo_start"]), S_CELL_R),
                P(fmt_km(r["odo_end"]), S_CELL_R),
                P(fmt_km(r["km"]), S_CELL_R),
                P(r["type_label"]),
                P(purpose_text),
                P(comment),
            ]
        )
    if subtotal_label is not None:
        data.append([P(subtotal_label, S_CELL_B), "", "", "", "", "", P(fmt_km(round(total, 1)), S_CELL_B), "", "", ""])
    t = Table(data, colWidths=[w * mm for w in COL_W], repeatRows=1)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f8fafc")))
    if subtotal_label is not None:
        style.append(("SPAN", (0, len(data) - 1), (5, len(data) - 1)))
        style.append(("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), colors.HexColor("#e2e8f0")))
    t.setStyle(TableStyle(style))
    return t


def _issues_block(summary: dict) -> list:
    out = []
    rec_parts = []
    if summary.get("by_odometer_km") is not None:
        rec_parts.append(
            f"Jakson kilometrit mittarilukemista {fmt_km(summary['by_odometer_km'])} km, kirjatut ajot "
            f"{fmt_km(summary['logged_km'])} km, erotus {fmt_km(summary['difference_km'])} km."
        )
    if summary.get("ha_odometer_at_start") is not None:
        rec_parts.append(f"HA:n mittarilukema jakson vaihteessa: {fmt_km(summary['ha_odometer_at_start'])} km.")
    if rec_parts:
        out.append(P(" ".join(rec_parts), S_BODY))
    for issue in summary.get("issues") or []:
        if issue.kind == "gap" and issue.ack_reason:
            out.append(P(f"Kuitattu mittariaukko: {issue.message}. Peruste: {issue.ack_reason}", S_BODY))
        else:
            out.append(P(f"Huomio: {issue.message}", S_WARN))
    return out


def _legend() -> Paragraph:
    return P(
        "Merkinnät: [käsin lisätty] = ajo lisätty käsin (ks. muutoshistoria), [korjattu] = autosta tullutta tietoa "
        "on korjattu perustellusti, [yhdistetty] = ajo on yhdistetty peräkkäisistä matkapätkistä. "
        "Kommentti (lisätieto) on vapaa lisätieto, joka ei korvaa ajon tarkoitusta. "
        "Työmatkat (asunnon ja työpaikan väliset ajot) luetaan verotuksessa yksityisajoiksi.",
        S_BODY,
    )


def build_month_pdf(
    ctx: ReportContext,
    year: int,
    month: int,
    flt: ReportFilter | None = None,
    footer_note: str = "",
    archive_note: str = "",
) -> bytes:
    flt = flt or ReportFilter()
    start, end = timeutil.month_bounds(year, month)
    period_label = f"{MONTH_NAMES[month]} {year} (1.{month}.–{_last_day(year, month)}.{month}.{year})"
    summary = ctx.month_summary(year, month)
    trips = ctx.trips(start, end, flt)
    rows = ctx.rows(trips)
    all_trips = ctx.db.list_trips(start, end)
    drivers = ctx.drivers(all_trips)

    story = [P(f"Ajopäiväkirja {month:02d}/{year}", S_TITLE)]
    if flt.is_partial:
        story.append(P(flt.title, S_PARTIAL))
        story.append(P("Osaraportti on lisätuloste. Virallinen jakson raportti sisältää kaikki ajot.", S_BODY))
    if archive_note:
        story.append(P(archive_note, S_BODY))
    story += [Spacer(1, 3 * mm), _info_table(ctx, start, period_label, summary, drivers), Spacer(1, 2 * mm)]
    story += _issues_block(summary)
    story.append(Spacer(1, 3 * mm))
    label = "Yhteensä" if not flt.is_partial else f"Yhteensä ({flt.title.replace('Osaraportti: ', '')})"
    if rows:
        story.append(_trip_table(rows, subtotal_label=label))
    else:
        story.append(P("Ei ajoja tällä jaksolla.", S_BODY))
    story += [Spacer(1, 3 * mm), _legend()]
    if not flt.is_partial:
        story += _consumption_section(ctx, start, end, summary.get("total_km"))
    return _render(ctx, story, f"{month:02d}/{year}", footer_note)


CONSUMPTION_HEADERS = ["Pvm", "Aika", "Tapahtuma", "Paikka", "Määrä", "Varaus / tankki", "Lähde", "Yksikköhinta", "Kommentti"]
CONSUMPTION_COL_W = [17, 20, 18, 64, 20, 26, 30, 20, 58]


def _consumption_lines(c: dict) -> list[tuple[str, str]]:
    lines = []
    charged = f"{fmt_qty(c['charged_kwh'])} kWh ({c['charge_count']} kpl)"
    if c["charged_estimated_kwh"]:
        charged += f", josta arvioitu {fmt_qty(c['charged_estimated_kwh'])} kWh"
    lines.append(("Ladattu sähköä", charged))
    if c["has_fuel"]:
        lines.append(("Tankattu polttoainetta", f"{fmt_qty(c['refuelled_l'], 1)} l ({c['refuel_count']} kpl)"))
        if c["fuel_used_l"] is not None:
            used = f"{fmt_qty(c['fuel_used_l'], 1)} l"
            if c["fuel_start_l"] is not None and c["fuel_end_l"] is not None:
                used += f" (tankissa {fmt_qty(c['fuel_start_l'], 0)} l → {fmt_qty(c['fuel_end_l'], 0)} l)"
            if c["fuel_per_100km"] is not None:
                used += f", {fmt_qty(c['fuel_per_100km'], 1)} l/100 km"
            lines.append(("Polttoainetta kulunut", used))
    return lines


def _consumption_section(ctx: ReportContext, start: str, end: str, total_km: float | None) -> list:
    rows = ctx.consumption_rows(start, end)
    c = ctx.consumption_summary(start, end, total_km)
    if not rows and c.get("fuel_used_l") in (None, 0):
        return []
    out = [Spacer(1, 4 * mm), P("Kulut (määrät)", S_H2)]
    summary = [[P(a, S_CELL_B), P(b)] for a, b in _consumption_lines(c)]
    st = Table(summary, colWidths=[52 * mm, 150 * mm])
    st.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
            ]
        )
    )
    out += [st, Spacer(1, 2 * mm)]
    if rows:
        data = [[P(h, S_CELL_B) for h in CONSUMPTION_HEADERS]]
        for r in rows:
            t = r["start"] + (f"–{r['end']}" if r["end"] and r["end"] != r["start"] else "")
            if r["end_date"] and r["end_date"] != r["date"]:
                t += f"\n(päättyi {r['end_date'][:6]})"
            price = f"{r['unit_price_text']} {r['unit_price_label']}" if r["unit_price_text"] else ""
            comment = "\n".join([x for x in [r["comment"]] + [f"[{m}]" for m in r["markers"]] if x])
            data.append(
                [
                    P(r["date"]),
                    P(t),
                    P(r["kind_label"]),
                    P(r["place"]),
                    P(f"{r['quantity_text']} {r['unit']}", S_CELL_R),
                    P(r["level"]),
                    P(r["source_label"]),
                    P(price, S_CELL_R),
                    P(comment),
                ]
            )
        t = Table(data, colWidths=[w * mm for w in CONSUMPTION_COL_W], repeatRows=1)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]
        t.setStyle(TableStyle(style))
        out.append(t)
    out += [
        Spacer(1, 2 * mm),
        P(
            "Määrät luetaan Home Assistantista: kotilatauksen energia laturin omasta mittarista (seinästä, sisältää "
            "lataushäviöt), muualla ladattu arvioidaan auton varaustason noususta. Tankkaus tunnistetaan tankin "
            "litramäärän noususta (auto ilmoittaa kokonaisia litroja, tarkkuus noin ±1 l). Yksikköhinta on käyttäjän "
            "itse lisäämä vapaaehtoinen tieto.",
            S_BODY,
        ),
    ]
    return out


def build_year_pdf(
    ctx: ReportContext,
    year: int,
    flt: ReportFilter | None = None,
    footer_note: str = "",
    include_changes: bool = True,
) -> bytes:
    flt = flt or ReportFilter()
    start, end = timeutil.year_bounds(year)
    summary = ctx.year_summary(year)
    all_trips = ctx.db.list_trips(start, end)
    drivers = ctx.drivers(all_trips)
    story = [P(f"Ajopäiväkirja – vuosiraportti {year}", S_TITLE)]
    if flt.is_partial:
        story.append(P(flt.title, S_PARTIAL))
        story.append(P("Osaraportti on lisätuloste. Virallinen vuosiraportti sisältää kaikki ajot.", S_BODY))
    story += [Spacer(1, 3 * mm), _info_table(ctx, start, f"1.1.–31.12.{year}", summary, drivers), Spacer(1, 2 * mm)]
    story += _issues_block(summary)

    story.append(P("Kuukausierittely", S_H2))
    mdata = [[P(h, S_CELL_B) for h in ("Kuukausi", "Alkulukema", "Loppulukema", "Työajot km", "Yksityisajot km", "josta työmatkat km", "Yhteensä km", "Tila")]]
    for m in summary["months"]:
        mdata.append(
            [
                P(m["name"]),
                P(fmt_km(m["odometer_start"]), S_CELL_R),
                P(fmt_km(m["odometer_end"]), S_CELL_R),
                P(fmt_km(m["business_km"]), S_CELL_R),
                P(fmt_km(m["private_all_km"]), S_CELL_R),
                P(fmt_km(m["commute_km"]), S_CELL_R),
                P(fmt_km(m["total_km"]), S_CELL_R),
                P("lukittu" if m["locked"] else "avoin"),
            ]
        )
    mdata.append(
        [
            P("Yhteensä", S_CELL_B),
            P(fmt_km(summary["odometer_start"]), S_CELL_R),
            P(fmt_km(summary["odometer_end"]), S_CELL_R),
            P(fmt_km(summary["business_km"]), S_CELL_B),
            P(fmt_km(summary["private_all_km"]), S_CELL_B),
            P(fmt_km(summary["commute_km"]), S_CELL_B),
            P(fmt_km(summary["total_km"]), S_CELL_B),
            P(""),
        ]
    )
    mt = Table(mdata, colWidths=[30 * mm, 28 * mm, 28 * mm, 26 * mm, 30 * mm, 32 * mm, 26 * mm, 20 * mm], repeatRows=1)
    mt.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
            ]
        )
    )
    story.append(mt)
    if not flt.is_partial:
        story += _year_consumption_table(ctx, year, summary)

    filtered_total = 0.0
    for m in range(1, 13):
        ms, me = timeutil.month_bounds(year, m)
        rows = ctx.rows(ctx.trips(ms, me, flt))
        if not rows:
            continue
        filtered_total += sum(r["km"] or 0 for r in rows)
        story += [PageBreak(), P(f"{MONTH_NAMES[m]} {year}", S_H2), _trip_table(rows, subtotal_label=f"{MONTH_NAMES[m]} yhteensä")]
    if flt.is_partial:
        story += [Spacer(1, 4 * mm), P(f"Jakson summa ({flt.title.replace('Osaraportti: ', '')}): {fmt_km(round(filtered_total, 1))} km", S_H2)]
    story += [Spacer(1, 3 * mm), _legend()]
    if include_changes:
        story += _changes_section(ctx, start, end)
    return _render(ctx, story, str(year), footer_note)


def _year_consumption_table(ctx: ReportContext, year: int, summary: dict) -> list:
    has_fuel = ctx.vehicle.has_fuel
    head = ["Kuukausi", "Ladattu kWh", "josta arvio kWh", "Latauksia"]
    if has_fuel:
        head += ["Tankattu l", "Tankkauksia", "Kulunut l", "l/100 km"]
    data = [[P(h, S_CELL_B) for h in head]]
    tot = {"kwh": 0.0, "est": 0.0, "n": 0, "l": 0.0, "nl": 0, "used": 0.0}
    any_data = False
    for m in summary["months"]:
        ms, me = timeutil.month_bounds(year, m["month"])
        c = ctx.consumption_summary(ms, me, m["total_km"])
        any_data = any_data or c["charge_count"] or c["refuel_count"] or c["fuel_used_l"]
        row = [P(m["name"]), P(fmt_qty(c["charged_kwh"]), S_CELL_R), P(fmt_qty(c["charged_estimated_kwh"]), S_CELL_R), P(c["charge_count"], S_CELL_R)]
        tot["kwh"] += c["charged_kwh"]
        tot["est"] += c["charged_estimated_kwh"]
        tot["n"] += c["charge_count"]
        if has_fuel:
            row += [
                P(fmt_qty(c["refuelled_l"], 1), S_CELL_R),
                P(c["refuel_count"], S_CELL_R),
                P(fmt_qty(c["fuel_used_l"], 1), S_CELL_R),
                P(fmt_qty(c["fuel_per_100km"], 1), S_CELL_R),
            ]
            tot["l"] += c["refuelled_l"]
            tot["nl"] += c["refuel_count"]
            tot["used"] += c["fuel_used_l"] or 0
        data.append(row)
    if not any_data:
        return []
    total_row = [P("Yhteensä", S_CELL_B), P(fmt_qty(tot["kwh"]), S_CELL_B), P(fmt_qty(tot["est"]), S_CELL_B), P(tot["n"], S_CELL_B)]
    if has_fuel:
        per100 = tot["used"] / summary["total_km"] * 100 if summary.get("total_km") else None
        total_row += [P(fmt_qty(tot["l"], 1), S_CELL_B), P(tot["nl"], S_CELL_B), P(fmt_qty(tot["used"], 1), S_CELL_B), P(fmt_qty(per100, 1), S_CELL_B)]
    data.append(total_row)
    widths = [30, 26, 28, 22] + ([24, 24, 24, 22] if has_fuel else [])
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
            ]
        )
    )
    return [P("Kulut kuukausittain (määrät)", S_H2), t]


CHANGE_LABELS = {
    "trip": "Ajo lisätty käsin",
    "gap_ack": "Mittariaukko kuitattu",
    "period_lock": "Jakson lukitus",
    "comment": "Kommentti",
    "start_time": "Alkuaika korjattu",
    "end_time": "Loppuaika korjattu",
    "start_address": "Lähtöpaikka korjattu",
    "end_address": "Määränpää korjattu",
    "start_odometer": "Alkulukema korjattu",
    "end_odometer": "Loppulukema korjattu",
}


def _changes_section(ctx: ReportContext, start: str, end: str) -> list:
    changes = []
    for ch in ctx.db.changes_between(start, end):
        f = ch["field"]
        relevant = (
            f in CORRECTION_FIELDS
            or f in ("trip", "gap_ack", "period_lock")
            or f.startswith("vehicle_info.")
            or (f == "comment" and "lukituksen jälkeen" in (ch["reason"] or ""))
            or (f.startswith("consumption:") and not f.endswith((":unit_price", ":comment")))
        )
        if relevant:
            changes.append(ch)
    if not changes:
        return []
    data = [[P(h, S_CELL_B) for h in ("Aika", "Matka", "Muutos", "Vanha arvo", "Uusi arvo", "Lähde", "Perustelu")]]
    for ch in changes:
        label = CHANGE_LABELS.get(ch["field"], ch["field"])
        if ch["field"].startswith("vehicle_info."):
            label = "Autotieto muuttui: " + ch["field"].split(".", 1)[1]
        elif ch["field"].startswith("consumption:"):
            parts = ch["field"].split(":")
            what = {"quantity": "määrä korjattu", "voided": "poistettu laskennasta"}.get(parts[2] if len(parts) > 2 else "", "kirjattu käsin")
            label = f"Kulu {parts[1]}: {what}"
        old, new = ch["old_value"] or "", ch["new_value"] or ""
        if ch["field"] in ("start_time", "end_time"):
            old, new = timeutil.fmt_datetime(old) if old else "", timeutil.fmt_datetime(new) if new else ""
        data.append(
            [
                P(timeutil.fmt_datetime(ch["changed_at"])),
                P(ch["trip_id"] or ""),
                P(label),
                P(old),
                P(new),
                P(ch["source"]),
                P(ch["reason"] or ""),
            ]
        )
    t = Table(data, colWidths=[26 * mm, 14 * mm, 40 * mm, 50 * mm, 50 * mm, 22 * mm, 71 * mm], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return [PageBreak(), P("Muutoshistoria", S_H2), P("Käsin lisätyt ajot, korjaukset, kuitatut aukot, lukitukset ja autotietojen muutokset (muutoslokista, jota ei voi muokata).", S_BODY), Spacer(1, 2 * mm), t]


def _render(ctx: ReportContext, story: list, period_label: str, footer_note: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=PAGE,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=16 * mm,
        bottomMargin=13 * mm,
        title=f"Ajopäiväkirja {ctx.vehicle.id} {ctx.vehicle.plate} {period_label}",
        author="CarData Logbook",
        creator=f"CarData Logbook v{APP_VERSION}",
    )
    deco = _page_decorator(ctx, period_label, footer_note)
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def _last_day(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


__all__ = ["build_month_pdf", "build_year_pdf", "KeepTogether", "summarize"]
