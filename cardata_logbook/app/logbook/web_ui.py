"""Selainkäyttöliittymä (FastAPI + Jinja2), näkyy HA:n sivupalkissa Ingressin kautta.

Kaikki näkymät ovat auton polun alla (/v/<auto>/...). "Kaikki autot" -näkymää ei ole.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import timeutil
from .csv_report import build_csv
from .database import EDITABLE_FIELDS, TRIP_TYPE_LABELS, LogbookError
from .integrity import purpose_issues, summarize
from .pdf_report import build_month_pdf, build_year_pdf
from .report_data import APP_VERSION, MONTH_NAMES, ReportFilter, consumption_row, fmt_km, fmt_qty, trip_row
from .discovery import suggest_id
from .service import LockBlocked, VehicleService
from .vehicle_registry import (
    FUEL_TYPE_LABELS,
    FUEL_TYPES,
    POWERTRAIN_LABELS,
    POWERTRAINS,
    ROLES,
    VehicleConfig,
    settings_to_store,
    validate_vehicle,
    vehicle_from_store,
    vehicle_to_store,
)
from .xlsx_report import ImportRejected, build_xlsx

HERE = Path(__file__).parent
INGRESS_IP = "172.30.32.2"

FIELD_LABELS = {
    "start_time": "Alkuaika",
    "end_time": "Loppuaika",
    "start_address": "Lähtöpaikka",
    "end_address": "Määränpää",
    "start_odometer": "Mittari alussa",
    "end_odometer": "Mittari lopussa",
    "start_lat": "Lähtö lat",
    "start_lon": "Lähtö lon",
    "end_lat": "Määränpää lat",
    "end_lon": "Määränpää lon",
    "trip_type": "Ajotyyppi",
    "purpose": "Tarkoitus",
    "comment": "Kommentti",
    "trip": "Ajo lisätty",
    "merged_into_id": "Yhdistetty matkaan",
    "merge": "Yhdistetty matkoista",
    "unmerge": "Yhdistäminen purettu",
    "gap_ack": "Mittariaukko kuitattu",
    "period_lock": "Jakson lukitus",
}
ROLE_LABELS = {
    "location": "Sijainti",
    "odometer": "Mittarilukema",
    "motion": "Liiketila",
    "soc": "Ajoakun varaus",
    "charging_state": "Lataustila",
    "charging_method": "Latausmenetelmä (kytkentä)",
    "hv_system_state": "Ajoakun järjestelmän tila",
    "fuel_level": "Polttoainetta tankissa (l)",
    "charger_energy": "Laturin energia",
}
SOURCE_NAMES = {"manual": "käsin", "auto": "löydetty", "charger": "kotilaturi", "": ""}
SOURCE_LABELS = {
    "ui": "selain",
    "excel_drive": "Excel (Drive)",
    "excel_upload": "Excel (selain)",
    "merge": "yhdistäminen",
    "unmerge": "purku",
    "manual_add": "käsin lisäys",
    "gap_fill": "aukon täyttö",
    "period": "jakso",
    "vehicle_info": "asetussivu",
    "consumption": "kulu",
}


def create_app(app_state, restrict: bool | None = None) -> FastAPI:
    api = FastAPI(title="CarData Logbook", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters["date"] = timeutil.fmt_date
    env.filters["time"] = timeutil.fmt_time
    env.filters["dt"] = timeutil.fmt_datetime
    env.filters["km"] = fmt_km
    env.globals.update(
        TYPE_LABELS=TRIP_TYPE_LABELS,
        MONTH_NAMES=MONTH_NAMES,
        FIELD_LABELS=FIELD_LABELS,
        SOURCE_LABELS=SOURCE_LABELS,
        ROLES=ROLES,
        ROLE_LABELS=ROLE_LABELS,
        SOURCE_NAMES=SOURCE_NAMES,
        POWERTRAINS=POWERTRAINS,
        POWERTRAIN_LABELS=POWERTRAIN_LABELS,
        FUEL_TYPES=FUEL_TYPES,
        FUEL_TYPE_LABELS=FUEL_TYPE_LABELS,
        APP_VERSION=APP_VERSION,
        qty=fmt_qty,
    )
    api.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    if restrict is None:
        restrict = bool(os.environ.get("SUPERVISOR_TOKEN"))

    @api.middleware("http")
    async def ingress_only(request: Request, call_next):
        if restrict:
            host = request.client.host if request.client else ""
            if host not in (INGRESS_IP, "127.0.0.1"):
                return Response("Käytä ajopäiväkirjaa Home Assistantin kautta.", status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------------ apu
    def base(request: Request) -> str:
        return request.headers.get("X-Ingress-Path", "").rstrip("/")

    def svc_or_404(vid: str) -> VehicleService:
        svc = app_state.services.get(vid)
        if svc is None:
            raise HTTPException(404, f"Autoa '{vid}' ei ole käytössä.")
        return svc

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("err", request.query_params.get("err"))
        ctx["base"] = base(request)
        ctx["app"] = app_state
        ctx["vehicles"] = app_state.all_vehicles()
        return templates.TemplateResponse(request, name, ctx)

    def redirect(request: Request, path: str, msg: str | None = None, err: str | None = None, **params) -> RedirectResponse:
        q = {k: v for k, v in params.items() if v not in (None, "")}
        if msg:
            q["msg"] = msg
        if err:
            q["err"] = err
        url = base(request) + path + (("?" + urlencode(q)) if q else "")
        return RedirectResponse(url, status_code=303)

    def attachment(data: bytes, filename: str, media: str) -> Response:
        return Response(
            data,
            media_type=media,
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
        )

    def default_period() -> tuple[int, int]:
        now = timeutil.now_utc().astimezone(timeutil.local_tz())
        return now.year, now.month

    # ------------------------------------------------------------ etusivu
    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        cards = []
        for v in app_state.all_vehicles():
            svc = app_state.services.get(v.id)
            last = svc.db.last_trip() if svc else None
            cards.append(
                {
                    "v": v,
                    "svc": svc,
                    "status": app_state.vehicle_status(v.id) if svc else ("Ei käytössä" if not v.enabled else "Ei konfiguroitu"),
                    "errors": app_state.vehicle_errors.get(v.id, []),
                    "last": last,
                }
            )
        return render(request, "index.html", cards=cards)

    # -------------------------------------------------------- matkalista
    @api.get("/v/{vid}", response_class=HTMLResponse)
    async def trip_list(request: Request, vid: str, year: int | None = None, month: int | None = None, type: str = ""):
        svc = svc_or_404(vid)
        dy, dm = default_period()
        year = year or dy
        month = dm if month is None and year == dy else (month if month is not None else 0)
        if month:
            start, end = timeutil.month_bounds(year, month)
        else:
            start, end = timeutil.year_bounds(year)
        types = [type] if type in TRIP_TYPE_LABELS else None
        trips = svc.db.list_trips(start, end, trip_types=types)
        all_in_period = svc.db.list_trips(start, end)
        rows = [trip_row(svc.db, t) for t in trips]
        locked = {}
        for t in trips:
            ym = timeutil.local_year_month(t["start_time"])
            if ym not in locked:
                locked[ym] = svc.db.is_locked(*ym)
        for r, t in zip(rows, trips):
            r["locked"] = locked[timeutil.local_year_month(t["start_time"])]
            r["is_parent"] = bool(t["is_merged_parent"])
        issues = svc.issues(start, end)
        gaps_by_next = {i.next_trip_id: i for i in issues if i.kind in ("gap", "overlap")}
        other_issues = [i for i in issues if i.kind not in ("gap", "overlap")] + purpose_issues(all_in_period)
        years = sorted({timeutil.local_year_month(t["start_time"])[0] for t in svc.db.list_trips()} | {dy})
        return render(
            request,
            "trips.html",
            svc=svc,
            v=svc.vehicle,
            rows=rows,
            year=year,
            month=month,
            type=type,
            years=years,
            summary=summarize(all_in_period),
            gaps_by_next=gaps_by_next,
            other_issues=other_issues,
            status=app_state.vehicle_status(vid),
            stream_ok=app_state.stream_ok,
        )

    @api.post("/v/{vid}/trips/{tid}/field")
    async def update_field(request: Request, vid: str, tid: int):
        svc = svc_or_404(vid)
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
        else:
            body = dict(await request.form())
        field = str(body.get("field", ""))
        if field not in ("trip_type", "purpose", "comment"):
            return JSONResponse({"ok": False, "error": "Tätä kenttää ei voi muokata pikamuokkauksella."}, status_code=400)
        try:
            trip = svc.db.update_field(tid, field, body.get("value"), source="ui")
        except LogbookError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        svc.mark_dirty_trip(tid)
        return {"ok": True, "trip": {k: trip[k] for k in ("id", "trip_type", "purpose", "comment")}}

    @api.post("/v/{vid}/trips/merge")
    async def merge(request: Request, vid: str):
        svc = svc_or_404(vid)
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
        back = dict(year=form.get("year"), month=form.get("month"))
        try:
            new_id = svc.db.merge(ids)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}", err=str(exc), **back)
        svc.mark_dirty_trip(new_id)
        return redirect(request, f"/v/{vid}", msg=f"{len(ids)} matkaa yhdistetty.", **back)

    @api.post("/v/{vid}/trips/{tid}/unmerge")
    async def unmerge(request: Request, vid: str, tid: int):
        svc = svc_or_404(vid)
        trip = svc.db.get_trip(tid)
        try:
            kids = svc.db.unmerge(tid)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}", err=str(exc))
        if trip:
            svc.mark_dirty_time(trip["start_time"])
        y, m = timeutil.local_year_month(trip["start_time"]) if trip else default_period()
        return redirect(request, f"/v/{vid}", msg=f"Yhdistäminen purettu ({len(kids)} matkaa).", year=y, month=m)

    @api.get("/v/{vid}/trips/{tid}", response_class=HTMLResponse)
    async def trip_detail(request: Request, vid: str, tid: int):
        svc = svc_or_404(vid)
        trip = svc.db.get_trip(tid)
        if trip is None:
            raise HTTPException(404, "Matkaa ei löydy tämän auton ajopäiväkirjasta.")
        return render(
            request,
            "trip.html",
            svc=svc,
            v=svc.vehicle,
            trip=trip,
            row=trip_row(svc.db, trip),
            history=svc.db.history(tid),
            children=svc.db.children(tid),
            stops=svc.db.charging_stops(tid),
            locked=svc.db.trip_is_locked(trip),
            year_month=timeutil.local_year_month(trip["start_time"]),
        )

    @api.post("/v/{vid}/trips/{tid}/correct")
    async def correct(request: Request, vid: str, tid: int, field: str = Form(...), value: str = Form(""), reason: str = Form("")):
        svc = svc_or_404(vid)
        if field not in EDITABLE_FIELDS:
            return redirect(request, f"/v/{vid}/trips/{tid}", err="Tuntematon kenttä.")
        if field in ("start_time", "end_time") and value:
            value = timeutil.local_naive_to_iso(value)
        try:
            svc.db.update_field(tid, field, value, source="ui", reason=reason)
        except (LogbookError, ValueError) as exc:
            return redirect(request, f"/v/{vid}/trips/{tid}", err=str(exc))
        svc.mark_dirty_trip(tid)
        return redirect(request, f"/v/{vid}/trips/{tid}", msg="Korjaus tallennettu ja kirjattu muutoslokiin.")

    # ------------------------------------------------------ käsin lisäys
    @api.get("/v/{vid}/new", response_class=HTMLResponse)
    async def new_trip_form(request: Request, vid: str, prev: int | None = None, next: int | None = None):
        svc = svc_or_404(vid)
        prefill = {}
        if prev and next:
            p, n = svc.db.get_trip(prev), svc.db.get_trip(next)
            if p and n:
                prefill = {
                    "start_time": timeutil.to_local(p["end_time"]).strftime("%Y-%m-%dT%H:%M"),
                    "end_time": timeutil.to_local(n["start_time"]).strftime("%Y-%m-%dT%H:%M"),
                    "start_address": p["end_address"],
                    "end_address": n["start_address"],
                    "start_odometer": p["end_odometer"],
                    "end_odometer": n["start_odometer"],
                }
        return render(request, "new_trip.html", svc=svc, v=svc.vehicle, prefill=prefill, prev=prev, next=next)

    @api.post("/v/{vid}/new")
    async def new_trip(
        request: Request,
        vid: str,
        start_time: str = Form(...),
        end_time: str = Form(...),
        start_address: str = Form(""),
        end_address: str = Form(""),
        start_odometer: str = Form(""),
        end_odometer: str = Form(""),
        trip_type: str = Form("private"),
        purpose: str = Form(""),
        comment: str = Form(""),
        reason: str = Form(""),
        prev: str = Form(""),
        next: str = Form(""),
    ):
        svc = svc_or_404(vid)
        try:
            so = float(start_odometer.replace(",", ".")) if start_odometer else None
            eo = float(end_odometer.replace(",", ".")) if end_odometer else None
            if so is None or eo is None:
                raise LogbookError("Anna mittarilukemat ajon alussa ja lopussa.")
            if eo < so:
                raise LogbookError("Loppulukema ei voi olla pienempi kuin alkulukema.")
            trip = {
                "start_time": timeutil.local_naive_to_iso(start_time),
                "end_time": timeutil.local_naive_to_iso(end_time),
                "start_address": start_address,
                "end_address": end_address,
                "start_odometer": so,
                "end_odometer": eo,
                "trip_type": trip_type,
                "purpose": purpose,
                "comment": comment,
            }
            if trip["end_time"] < trip["start_time"]:
                raise LogbookError("Loppuaika on ennen alkuaikaa.")
            if prev and next:
                tid = svc.db.insert_trip(trip, source="gap_fill", reason=reason or "Mittariaukosta luotu ajo")
                svc.mark_dirty_time(trip["start_time"])
            else:
                tid = svc.add_manual_trip(trip, reason=reason)
        except (LogbookError, ValueError) as exc:
            return redirect(request, f"/v/{vid}/new", err=str(exc), prev=prev, next=next)
        y, m = timeutil.local_year_month(trip["start_time"])
        return redirect(request, f"/v/{vid}", msg=f"Ajo lisätty (matka {tid}).", year=y, month=m)

    @api.post("/v/{vid}/gaps/ack")
    async def ack_gap(request: Request, vid: str, prev: int = Form(...), next: int = Form(...), reason: str = Form("")):
        svc = svc_or_404(vid)
        try:
            svc.db.ack_gap(prev, next, reason)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}", err=str(exc))
        svc.mark_dirty_trip(next)
        trip = svc.db.get_trip(next)
        y, m = timeutil.local_year_month(trip["start_time"])
        return redirect(request, f"/v/{vid}", msg="Aukko kuitattu ja kirjattu muutoslokiin.", year=y, month=m)

    # --------------------------------------------------------------- kulut
    @api.get("/v/{vid}/costs", response_class=HTMLResponse)
    async def costs(request: Request, vid: str, year: int | None = None, month: int | None = None):
        svc = svc_or_404(vid)
        dy, dm = default_period()
        year = year or dy
        month = dm if month is None and year == dy else (month if month is not None else 0)
        start, end = timeutil.month_bounds(year, month) if month else timeutil.year_bounds(year)
        events = svc.db.list_consumption(start, end)
        rows = []
        for e in events:
            r = consumption_row(e)
            r["locked"] = svc.db.is_locked(*timeutil.local_year_month(e["start_time"]))
            rows.append(r)
        km = summarize(svc.db.list_trips(start, end))["total_km"]
        years = sorted({timeutil.local_year_month(t["start_time"])[0] for t in svc.db.list_trips()} | {dy})
        return render(
            request,
            "costs.html",
            svc=svc,
            v=svc.vehicle,
            rows=rows,
            c=svc.ctx.consumption_summary(start, end, km),
            year=year,
            month=month,
            years=years,
            snapshot=svc.consumption.snapshot() if svc.consumption else {},
        )

    @api.post("/v/{vid}/costs/{eid}/field")
    async def cost_field(request: Request, vid: str, eid: int):
        svc = svc_or_404(vid)
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
        else:
            body = dict(await request.form())
        field = str(body.get("field", ""))
        if field not in ("unit_price", "comment"):
            return JSONResponse({"ok": False, "error": "Tätä kenttää ei voi muokata pikamuokkauksella."}, status_code=400)
        try:
            ev = svc.update_consumption(eid, field, body.get("value"), source="ui")
        except LogbookError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "event": {k: ev[k] for k in ("id", "unit_price", "comment")}}

    @api.post("/v/{vid}/costs/{eid}/correct")
    async def cost_correct(
        request: Request,
        vid: str,
        eid: int,
        quantity: str = Form(""),
        voided: str = Form(""),
        reason: str = Form(""),
        year: str = Form(""),
        month: str = Form(""),
    ):
        svc = svc_or_404(vid)
        back = dict(year=year, month=month)
        try:
            if voided:
                svc.update_consumption(eid, "voided", "1", source="ui", reason=reason)
                return redirect(request, f"/v/{vid}/costs", msg="Kulu poistettu laskennasta ja kirjattu muutoslokiin.", **back)
            svc.update_consumption(eid, "quantity", quantity, source="ui", reason=reason)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}/costs", err=str(exc), **back)
        return redirect(request, f"/v/{vid}/costs", msg="Korjaus tallennettu ja kirjattu muutoslokiin.", **back)

    @api.post("/v/{vid}/costs/new")
    async def cost_new(
        request: Request,
        vid: str,
        kind: str = Form(...),
        start_time: str = Form(...),
        quantity: str = Form(...),
        address: str = Form(""),
        unit_price: str = Form(""),
        comment: str = Form(""),
        reason: str = Form(""),
    ):
        svc = svc_or_404(vid)
        try:
            if kind == "refuel" and not svc.vehicle.has_fuel:
                raise LogbookError("Sähköautolle ei voi kirjata tankkausta.")
            try:
                q = float(quantity.replace(",", "."))
                price = float(unit_price.replace(",", ".")) if unit_price.strip() else None
            except ValueError as exc:
                raise LogbookError("Määrän ja yksikköhinnan on oltava lukuja.") from exc
            ts = timeutil.local_naive_to_iso(start_time)
            svc.add_manual_consumption(
                {"kind": kind, "start_time": ts, "quantity": q, "address": address, "unit_price": price, "comment": comment},
                reason=reason,
            )
        except (LogbookError, ValueError) as exc:
            return redirect(request, f"/v/{vid}/costs", err=str(exc))
        y, m = timeutil.local_year_month(ts)
        return redirect(request, f"/v/{vid}/costs", msg="Kulu kirjattu.", year=y, month=m)

    # ------------------------------------------------------------ raportit
    @api.get("/v/{vid}/reports", response_class=HTMLResponse)
    async def reports(request: Request, vid: str, year: int | None = None):
        svc = svc_or_404(vid)
        dy, dm = default_period()
        year = year or dy
        months = []
        for m in range(1, 13):
            p = svc.db.period(year, m)
            ms, me = timeutil.month_bounds(year, m)
            trips = svc.db.list_trips(ms, me)
            months.append(
                {
                    "month": m,
                    "name": MONTH_NAMES[m],
                    "period": p,
                    "count": len(trips),
                    "km": summarize(trips)["total_km"],
                    "ended": timeutil.parse_iso(me) <= timeutil.now_utc(),
                    "issues": [i for i in svc.issues(ms, me) if i.blocking] + purpose_issues(trips),
                }
            )
        years = sorted({timeutil.local_year_month(t["start_time"])[0] for t in svc.db.list_trips()} | {dy})
        return render(
            request,
            "reports.html",
            svc=svc,
            v=svc.vehicle,
            year=year,
            default_month=dm if year == dy else 12,
            years=years,
            months=months,
            imports=svc.db.imports(10),
            conflicts=svc.db.open_conflicts(),
            commute_default=svc.settings.commute_counts_as_private,
        )

    @api.get("/v/{vid}/download")
    async def download(
        request: Request,
        vid: str,
        kind: str,
        year: int,
        month: int = 0,
        filter: str = "all",
        include_commute: str | None = None,
    ):
        svc = svc_or_404(vid)
        inc = None if include_commute is None else include_commute in ("1", "on", "true")
        flt = ReportFilter.parse(filter, inc, svc.settings)
        ctx = svc.ctx
        if kind == "month_pdf":
            if not 1 <= month <= 12:
                raise HTTPException(400, "Valitse kuukausi.")
            archived = svc.period_file(year, month) if not flt.is_partial else None
            if archived is not None:
                return FileResponse(archived, media_type="application/pdf", filename=archived.name)
            data = build_month_pdf(ctx, year, month, flt)
            return attachment(data, ctx.month_pdf_name(year, month, flt), "application/pdf")
        if kind == "year_pdf":
            return attachment(build_year_pdf(ctx, year, flt), ctx.year_pdf_name(year, flt), "application/pdf")
        if kind == "xlsx":
            return attachment(
                build_xlsx(ctx, year, flt),
                ctx.xlsx_name(year, flt),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        if kind == "csv":
            return attachment(build_csv(ctx, year, flt), ctx.csv_name(year, flt), "text/csv; charset=utf-8")
        raise HTTPException(400, "Tuntematon raporttityyppi.")

    @api.post("/v/{vid}/import")
    async def upload(request: Request, vid: str, file: UploadFile = File(...)):
        svc = svc_or_404(vid)
        data = await file.read()
        try:
            res = svc.import_bytes(data, file.filename or "tuonti.xlsx", source="excel_upload")
        except ImportRejected as exc:
            return redirect(request, f"/v/{vid}/reports", err=f"Tuonti hylätty: {exc}")
        return redirect(request, f"/v/{vid}/reports", msg=f"Tuonti valmis: {res.status_text()}.")

    @api.post("/v/{vid}/conflicts/{cid}")
    async def resolve(request: Request, vid: str, cid: int, choice: str = Form(...)):
        svc = svc_or_404(vid)
        try:
            svc.db.resolve_conflict(cid, choice)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}/reports", err=str(exc))
        return redirect(request, f"/v/{vid}/reports", msg="Ristiriita ratkaistu.")

    @api.post("/v/{vid}/periods/lock")
    async def lock(request: Request, vid: str, year: int = Form(...), month: int = Form(...)):
        svc = svc_or_404(vid)
        try:
            p = svc.lock_period(year, month)
        except LockBlocked as exc:
            return redirect(request, f"/v/{vid}/reports", err=str(exc), year=year)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}/reports", err=str(exc), year=year)
        return redirect(
            request,
            f"/v/{vid}/reports",
            msg=f"{MONTH_NAMES[month]} {year} lukittu. SHA-256: {p['report_sha256']}",
            year=year,
        )

    @api.post("/v/{vid}/periods/unlock")
    async def unlock(request: Request, vid: str, year: int = Form(...), month: int = Form(...), reason: str = Form("")):
        svc = svc_or_404(vid)
        try:
            svc.unlock_period(year, month, reason)
        except LogbookError as exc:
            return redirect(request, f"/v/{vid}/reports", err=str(exc), year=year)
        return redirect(request, f"/v/{vid}/reports", msg="Lukitus avattu ja kirjattu muutoslokiin.", year=year)

    # ------------------------------------------------------------ asetukset
    @api.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        cars = []
        for v in app_state.all_vehicles():
            svc = app_state.services.get(v.id)
            cars.append(
                {
                    "v": v,
                    "svc": svc,
                    "errors": app_state.vehicle_errors.get(v.id, []),
                    "entities": svc.entity_status if svc else {},
                    "files": svc.exporter.recent_files() if svc else [],
                    "pending": [p.name for p in svc.exporter.pending_imports(0)] if svc else [],
                    "imports": svc.db.imports(5) if svc else [],
                    "snapshot": svc.trip_manager.snapshot() if svc and svc.trip_manager else {},
                    "info_history": svc.db.vehicle_info_history() if svc else [],
                }
            )
        return render(request, "settings.html", cars=cars, settings=app_state.settings)

    # --------------------------------------------------------------- autot
    def current_store() -> dict:
        data = app_state.store.load()
        return data if data is not None else settings_to_store(app_state.settings)

    def live_vehicle(raw: dict, home: str) -> VehicleConfig:
        """Lomakkeen auto, jolle on liitetty HA:sta löydetyt entiteetit (näytetään 'käytössä nyt')."""
        v = vehicle_from_store(raw, home)
        d = app_state.discovered.get(v.vin) if v.vin else None
        v.apply_discovered(d.entities if d else {})
        return v

    def energy_entities() -> list[str]:
        out = []
        states = app_state.ha.states if app_state.ha is not None else {}
        for eid, st in states.items():
            unit = str((st.get("attributes") or {}).get("unit_of_measurement") or "")
            if eid.startswith("sensor.") and unit in ("Wh", "kWh", "MWh"):
                out.append(eid)
        return sorted(out)

    @api.get("/vehicles", response_class=HTMLResponse)
    async def vehicles_page(request: Request, add: str = ""):
        store = current_store()
        home = store.get("home_charger_entity", "")
        forms = []
        for raw in store["vehicles"]:
            forms.append({"v": live_vehicle(raw, home), "new": False, "anchor": raw.get("id"), "errors": app_state.vehicle_errors.get(raw.get("id"), []), "hint": {}})
        by_vin = {str(r.get("vin") or ""): r.get("id") for r in store["vehicles"] if r.get("vin")}
        discovered = [{"d": d, "configured": by_vin.get(vin)} for vin, d in sorted(app_state.discovered.items())]
        d = app_state.discovered.get(add.strip().upper()) if add else None
        taken = {str(r.get("id")) for r in store["vehicles"]}
        if d is not None and d.vin not in by_vin:
            raw = {
                "id": suggest_id(d, taken),
                "make": d.brand,
                "model": d.model + (f" ({d.series})" if d.series and d.series not in d.model else ""),
                "powertrain": d.powertrain or "BEV",
                "vin": d.vin,
                "enabled": True,
                "auto_discover": True,
                "use_home_charger": bool(home) and (d.powertrain or "BEV") != "ICE",
            }
            forms.append({"v": live_vehicle(raw, home), "new": True, "anchor": "new", "errors": [], "hint": {}})
        else:
            forms.append({"v": live_vehicle({"id": "", "make": "", "powertrain": "BEV", "auto_discover": True}, home), "new": True, "anchor": "new", "errors": [], "hint": {}})
        return render(request, "vehicles.html", store=store, forms=forms, discovered=discovered, energy_entities=energy_entities())

    @api.post("/vehicles/save")
    async def vehicles_save(request: Request):
        form = await request.form()
        orig_id = str(form.get("orig_id") or "").strip()
        store = current_store()
        try:
            pause_min = int(str(form.get("charging_pause_min") or "0").strip() or 0)
            kwh_pct = float(str(form.get("charge_kwh_per_pct") or "0").replace(",", ".").strip() or 0)
        except ValueError:
            return redirect(request, "/vehicles", err="Lataustauon ja kWh/%-arvion on oltava numeroita.")
        raw = {
            "id": orig_id or str(form.get("id") or "").strip().lower(),
            "plate": str(form.get("plate") or ""),
            "make": str(form.get("make") or ""),
            "model": str(form.get("model") or ""),
            "powertrain": str(form.get("powertrain") or "BEV"),
            "fuel_type": str(form.get("fuel_type") or ""),
            "vin": str(form.get("vin") or "").strip().upper(),
            "driver": str(form.get("driver") or ""),
            "enabled": bool(form.get("enabled")),
            "charging_pause_enabled": pause_min > 0,
            "charging_pause_timeout_s": max(0, pause_min) * 60,
            "charge_kwh_per_pct": max(0.0, kwh_pct),
            "auto_discover": bool(form.get("auto_discover")),
            "use_home_charger": bool(form.get("use_home_charger")),
            "entities": {role: str(form.get(f"entity_{role}") or "").strip() for role in ROLES if str(form.get(f"entity_{role}") or "").strip()},
        }
        v = vehicle_from_store(raw, store.get("home_charger_entity", ""))
        errors = validate_vehicle(v)
        others = [r for r in store["vehicles"] if r.get("id") != orig_id]
        if not orig_id and any(r.get("id") == v.id for r in store["vehicles"]):
            errors.append(f"Tunniste '{v.id}' on jo käytössä.")
        if v.vin and any(str(r.get("vin") or "").upper() == v.vin for r in others):
            errors.append("Sama VIN on jo toisella autolla.")
        if orig_id and not any(r.get("id") == orig_id for r in store["vehicles"]):
            errors.append(f"Autoa '{orig_id}' ei löydy.")
        # Pakolliset entiteetit tarkistetaan HA:sta löydettyjen kanssa
        live = live_vehicle(raw, store.get("home_charger_entity", ""))
        if v.enabled and app_state.ha_connected:
            for role in ("location", "odometer"):
                if not live.entity(role):
                    errors.append(f"Roolille '{ROLE_LABELS[role]}' ei löytynyt entiteettiä. Tarkista VIN tai syötä entiteetti käsin.")
        if errors:
            return redirect(request, "/vehicles", err=" ".join(dict.fromkeys(errors)))
        clean = vehicle_to_store(v)
        if orig_id:
            store["vehicles"] = [clean if r.get("id") == orig_id else r for r in store["vehicles"]]
        else:
            store["vehicles"].append(clean)
        app_state.store.save(store)
        app_state.restart_needed = True
        return redirect(request, "/vehicles", msg=f"Auto {v.id} tallennettu.")

    @api.post("/vehicles/charger")
    async def vehicles_charger(request: Request, home_charger_entity: str = Form("")):
        store = current_store()
        store["home_charger_entity"] = home_charger_entity.strip()
        app_state.store.save(store)
        app_state.restart_needed = True
        return redirect(request, "/vehicles", msg="Kotilaturi tallennettu.")

    @api.post("/transfer/export")
    async def transfer_export(request: Request):
        from .transfer import export_package

        try:
            path = export_package(app_state.settings.data_dir, app_state.settings.export_dir, app_state.services)
        except Exception as exc:
            return redirect(request, "/settings", err=f"Siirtopaketin kirjoitus epäonnistui: {exc}")
        return redirect(request, "/settings", msg=f"Siirtopaketti kirjoitettu: {path}. Pysäytä tämä lisäosa ennen kuin käynnistät uuden.")

    @api.post("/restart")
    async def restart(request: Request):
        import asyncio
        import os as _os

        if not _os.environ.get("SUPERVISOR_TOKEN"):
            return redirect(request, "/vehicles", err="Uudelleenkäynnistys ei ole käytettävissä. Käynnistä lisäosa uudelleen Home Assistantin lisäosasivulta.")

        async def later():
            await asyncio.sleep(1)
            try:
                await app_state.restart()
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Uudelleenkäynnistys epäonnistui")

        try:
            asyncio.get_running_loop().create_task(later())
        except Exception as exc:
            return redirect(request, "/vehicles", err=f"Uudelleenkäynnistys epäonnistui ({exc}). Käynnistä lisäosa uudelleen Home Assistantin lisäosasivulta.")
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><p style='font-family:sans-serif'>Lisäosa käynnistyy uudelleen. "
            "Päivitä sivu noin puolen minuutin kuluttua.</p>"
        )

    @api.get("/health")
    async def health():
        return {"ok": True, "ha_connected": app_state.ha_connected, "version": APP_VERSION, "time": datetime.now().isoformat()}

    return api
