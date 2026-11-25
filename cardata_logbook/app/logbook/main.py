"""Add-onin pääohjelma: HA-yhteys, autokohtaiset tilakoneet, ajastetut tehtävät ja web-käyttöliittymä."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path

from . import timeutil
from .geocode import Geocoder
from .ha_client import HAClient, state_timestamp
from .service import VehicleService
from .consumption import ConsumptionTracker
from .trip_manager import NOT_CONFIGURED, STATUS_LABELS, Event, TripManager, Zone
from .discovery import DiscoveredVehicle, discover, find_stream_status
from .vehicle_registry import CONSUMPTION_ROLES, REQUIRED_ROLES, Settings, load_settings
from .vehicle_store import VehicleStore

# Kululaskennan tilakone tarvitsee nämä roolit (ajotunnistus ei käytä kulurooleja)
CONSUMPTION_INPUTS = set(CONSUMPTION_ROLES) | {"location", "soc", "charging_method"}

_LOG = logging.getLogger("logbook")

TICK_S = 15
GEOCODE_BATCH = 5


class LogbookApp:
    def __init__(self, settings: Settings, store: VehicleStore | None = None):
        self.settings = settings
        self.store = store or VehicleStore(settings.data_dir)
        # CarData-integraatiosta löydetyt autot VIN:n mukaan (päivittyy HA-yhteyden auetessa)
        self.discovered: dict[str, DiscoveredVehicle] = {}
        self.discovery_error: str | None = None
        self.restart_needed = False
        self.services: dict[str, VehicleService] = {}
        self.vehicle_errors: dict[str, list[str]] = dict(settings.vehicle_errors)
        # sama entiteetti (esim. kotilaturin energia) voi kuulua usealle autolle
        self.entity_map: dict[str, list[tuple[str, str]]] = {}
        self.zones: list[Zone] = []
        self.ha: HAClient | None = None
        self.ha_connected = False
        self.ha_error: str | None = None
        self.stream_ok = True
        self.stream_state: str | None = None
        self.mqtt = None
        self.geocoder = Geocoder(Path(settings.data_dir) / "geocache.db", settings.nominatim_user_agent)
        self._last_import_poll = 0.0
        self._last_daily: str | None = None
        self._last_month: tuple[int, int] | None = None
        self.started_at = timeutil.now_iso()
        for v in settings.active_vehicles():
            try:
                svc = VehicleService(v, settings)
                svc.trip_manager = TripManager(
                    v, settings, on_trip_finished=svc.on_trip_finished, state_path=svc.state_path, zones=self.zones
                )
                svc.consumption = ConsumptionTracker(
                    v,
                    settings,
                    on_charge=svc.on_consumption,
                    on_refuel=svc.on_consumption,
                    on_fuel_reading=svc.on_fuel_reading,
                    state_path=svc.consumption_state_path,
                    zones=self.zones,
                )
                self.services[v.id] = svc
            except Exception as exc:  # yhden auton vika ei estä toista
                _LOG.exception("Auton %s käynnistys epäonnistui", v.id)
                self.vehicle_errors.setdefault(v.id, []).append(str(exc))

    async def restart(self) -> None:
        """Käynnistää lisäosan uudelleen Supervisorin kautta (autojen muutokset voimaan)."""
        import aiohttp

        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN puuttuu")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://supervisor/addons/self/restart", headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Supervisor vastasi {resp.status}")

    # ------------------------------------------------------------- tilat
    def vehicle_status(self, vehicle_id: str) -> str:
        svc = self.services.get(vehicle_id)
        if svc is None or svc.entity_status.get("_not_configured"):
            return STATUS_LABELS[NOT_CONFIGURED]
        return svc.trip_manager.status_label

    def all_vehicles(self) -> list:
        return list(self.settings.vehicles)

    # ------------------------------------------------------------ HA-yhteys
    async def ha_loop(self) -> None:
        delay = 2
        while True:
            client = HAClient()
            try:
                await client.connect()
                self.ha = client
                await self._on_connected(client)
                self.ha_connected = True
                self.ha_error = None
                delay = 2
                await client.closed.wait()
                _LOG.warning("HA-yhteys katkesi, yhdistetään uudelleen")
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception as exc:
                self.ha_error = str(exc)
                _LOG.warning("HA-yhteysvirhe: %s (uusi yritys %s s päästä)", exc, delay)
            finally:
                self.ha_connected = False
                try:
                    await client.close()
                except Exception:
                    pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def _on_connected(self, client: HAClient) -> None:
        states = await client.get_states()
        by_id = {s["entity_id"]: s for s in states}
        self._update_zones(by_id)
        await self._discover(client, by_id)
        self.entity_map.clear()
        stream_eid = self.settings.stream_status_entity
        if stream_eid and stream_eid in by_id:
            self._set_stream(by_id[stream_eid]["state"])
        for vid, svc in self.services.items():
            status: dict[str, dict] = {}
            missing_required = False
            for role, eid in svc.vehicle.entities.items():
                if not eid:
                    continue
                st = by_id.get(eid)
                status[role] = {"entity_id": eid, "exists": st is not None, "state": st["state"] if st else None}
                if st is None:
                    msg = f"Entiteettiä {eid} ({role}) ei löydy Home Assistantista."
                    _LOG.warning("%s: %s", vid, msg)
                    if role in ("location", "odometer"):
                        missing_required = True
                else:
                    self.entity_map.setdefault(eid, []).append((vid, role))
            if any(not svc.vehicle.entity(r) for r in REQUIRED_ROLES):
                _LOG.warning("%s: pakollisia entiteettejä ei löytynyt (VIN %s)", vid, svc.vehicle.vin or "puuttuu")
                missing_required = True
            status["_not_configured"] = missing_required
            svc.entity_status = status
        queue: asyncio.Queue = asyncio.Queue()
        watch = list(self.entity_map) + ([stream_eid] if stream_eid in by_id else [])
        if not watch:
            _LOG.warning("Yhtään seurattavaa entiteettiä ei löytynyt")
            return
        await client.subscribe_entities(watch, lambda eid, st: queue.put_nowait((eid, st)))
        await self._backfill(client, by_id)
        asyncio.create_task(self._consume(queue, client))

    async def _discover(self, client: HAClient, by_id: dict) -> None:
        """Hakee autojen entiteetit CarData-integraatiosta VIN:n perusteella."""
        try:
            registry = await client.entity_registry()
            self.discovery_error = None
        except Exception as exc:
            _LOG.warning("Entiteettirekisteriä ei voitu lukea, automaattinen löytö ohitetaan: %s", exc)
            self.discovery_error = str(exc)
            return
        self.discovered = discover(registry, by_id)
        for vin, d in self.discovered.items():
            _LOG.info("CarData-auto löytyi: %s %s (%s), roolit: %s", d.display_name, vin[-6:], d.powertrain or "?", ", ".join(sorted(d.entities)))
        for v in self.settings.vehicles:
            if not v.vin:
                self._fill_vin(v)
            d = self.discovered.get(v.vin) if v.vin else None
            v.apply_discovered(d.entities if d else {})
            svc = self.services.get(v.id)
            if svc is not None and svc.trip_manager is not None:
                svc.trip_manager.has_motion = bool(v.entity("motion"))
        stream = self.settings.stream_status_entity
        if not stream or stream not in by_id:
            found = find_stream_status(registry)
            if found:
                self.settings.stream_status_entity = found

    def _fill_vin(self, v) -> None:
        """Täydentää VIN:n autolle, jonka käsin asetetut entiteetit kuuluvat löydettyyn CarData-autoon."""
        own = set(v.overrides.values())
        for vin, d in self.discovered.items():
            if own & set(d.entities.values()):
                v.vin = vin
                _LOG.info("%s: VIN täydennetty CarData-integraatiosta", v.id)
                try:
                    data = self.store.load()
                    if data is not None:
                        for raw in data["vehicles"]:
                            if raw.get("id") == v.id and not raw.get("vin"):
                                raw["vin"] = vin
                        self.store.save(data)
                except Exception:
                    _LOG.exception("%s: VIN:n tallennus epäonnistui", v.id)
                return

    def _update_zones(self, by_id: dict) -> None:
        self.zones.clear()
        for zid in self.settings.home_zones or []:
            st = by_id.get(zid)
            if not st:
                continue
            a = st.get("attributes") or {}
            if a.get("latitude") is not None and a.get("longitude") is not None:
                self.zones.append(Zone(zid, float(a["latitude"]), float(a["longitude"]), float(a.get("radius") or 100)))

    def _set_stream(self, state: str | None) -> None:
        self.stream_state = state
        self.stream_ok = state is None or str(state).lower() == "connected"
        for svc in self.services.values():
            svc.trip_manager.stream_ok = self.stream_ok

    async def _backfill(self, client: HAClient, by_id: dict) -> None:
        now = timeutil.now_utc()
        for vid, svc in self.services.items():
            tm = svc.trip_manager
            eids = [eid for eid, targets in self.entity_map.items() if any(v == vid for v, _ in targets)]
            if not eids:
                continue
            tm_last = timeutil.parse_iso(tm.last_ts) if tm.last_ts else None
            ct_last = timeutil.parse_iso(svc.consumption.last_ts) if svc.consumption and svc.consumption.last_ts else None
            last = min((t for t in (tm_last, ct_last) if t is not None), default=None)
            events: list[tuple[datetime, str, dict]] = []
            if last is not None and self.settings.backfill_max_hours > 0:
                start = max(last, now - timedelta(hours=self.settings.backfill_max_hours))
                try:
                    hist = await client.history(eids, start, now)
                except Exception as exc:
                    _LOG.warning("%s: historian haku epäonnistui: %s", vid, exc)
                    hist = {}
                for eid, rows in hist.items():
                    for st in rows:
                        ts = state_timestamp(st)
                        if ts > last:
                            events.append((ts, eid, st))
                _LOG.info("%s: historiapaikkaus %s tapahtumaa alkaen %s", vid, len(events), start)
            else:
                for eid in eids:
                    events.append((state_timestamp(by_id[eid]), eid, by_id[eid]))
            events.sort(key=lambda e: e[0])
            seed = ct_last is None and svc.consumption is not None
            if seed:
                # Kululaskenta käynnistyy ensimmäistä kertaa: aloitetaan nykytilasta (esim. auto on jo kytketty)
                current = sorted(((state_timestamp(by_id[e]), e) for e in eids if e in by_id), key=lambda x: x[0])
                for _, eid in current:
                    self._route(eid, by_id[eid], only=vid, skip_trip=True)
            for ts, eid, st in events:
                # kumpikin tilakone saa vain tapahtumat, joita se ei ole vielä käsitellyt
                self._route(
                    eid,
                    st,
                    only=vid,
                    skip_trip=tm_last is not None and ts <= tm_last,
                    skip_consumption=seed or (ct_last is not None and ts <= ct_last),
                )

    async def _consume(self, queue: asyncio.Queue, client: HAClient) -> None:
        while not client.closed.is_set():
            try:
                eid, st = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            try:
                self._route(eid, st)
            except Exception:
                _LOG.exception("Tapahtuman %s käsittely epäonnistui", eid)

    def _route(
        self, eid: str, st: dict, only: str | None = None, skip_trip: bool = False, skip_consumption: bool = False
    ) -> None:
        if eid == self.settings.stream_status_entity:
            self._set_stream(st.get("state"))
            return
        for vid, role in self.entity_map.get(eid) or []:
            if only is not None and vid != only:
                continue
            svc = self.services.get(vid)
            if svc is None:
                continue
            ev = Event(role=role, state=str(st.get("state", "")), attributes=st.get("attributes") or {}, ts=state_timestamp(st))
            if role not in CONSUMPTION_ROLES and not skip_trip:
                tm = svc.trip_manager
                if role == "odometer":
                    try:
                        value = float(ev.state)
                        svc.record_odometer_boundary(value, timeutil.to_iso(ev.ts), tm.s.odometer)
                    except ValueError:
                        pass
                try:
                    tm.process(ev)
                except Exception:
                    _LOG.exception("%s: tilakoneen virhe", vid)
            if role in CONSUMPTION_INPUTS and svc.consumption is not None and not skip_consumption:
                try:
                    svc.consumption.process(role, ev.state, ev.attributes, ev.ts)
                except Exception:
                    _LOG.exception("%s: kululaskennan virhe", vid)

    # --------------------------------------------------------- ajastetut
    async def periodic_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(TICK_S)
            now = timeutil.now_utc()
            local = now.astimezone(timeutil.local_tz())
            for vid, svc in self.services.items():
                try:
                    if self.ha_connected:
                        svc.trip_manager.tick(now)
                        if svc.consumption is not None:
                            svc.consumption.tick(now)
                    month = (local.year, local.month)
                    if self._last_month and month != self._last_month:
                        svc.mark_dirty_time(timeutil.month_bounds(*self._last_month)[0])
                        svc.mark_dirty_time(timeutil.month_bounds(*month)[0])
                        svc.dirty_since = now - timedelta(seconds=3600)
                    await loop.run_in_executor(None, svc.write_reports_if_due)
                except Exception:
                    _LOG.exception("%s: ajastettu tehtävä epäonnistui", vid)
            self._last_month = (local.year, local.month)
            if loop.time() - self._last_import_poll >= self.settings.xlsx_import_poll_s:
                self._last_import_poll = loop.time()
                for svc in self.services.values():
                    try:
                        await loop.run_in_executor(None, svc.process_import_folder)
                    except Exception:
                        _LOG.exception("%s: tuonti-kansion käsittely epäonnistui", svc.vehicle_id)
            day = local.strftime("%Y-%m-%d")
            if local.hour >= 3 and self._last_daily != day:
                self._last_daily = day
                for svc in self.services.values():
                    try:
                        await loop.run_in_executor(None, lambda s=svc: s.backup_db(check=True))
                        await loop.run_in_executor(None, svc.auto_lock)
                    except Exception:
                        _LOG.exception("%s: päivittäinen ylläpito epäonnistui", svc.vehicle_id)
            await self._geocode_pending()
            self._publish_mqtt()

    async def _geocode_pending(self) -> None:
        for svc in self.services.values():
            rows = svc.db.query(
                "SELECT id, start_lat, start_lon, end_lat, end_lon, start_address, end_address FROM trips"
                " WHERE ((start_address='' AND start_lat IS NOT NULL) OR (end_address='' AND end_lat IS NOT NULL))"
                " ORDER BY id DESC LIMIT ?",
                (GEOCODE_BATCH,),
            )
            for r in rows:
                for which, lat, lon in (("start_address", r["start_lat"], r["start_lon"]), ("end_address", r["end_lat"], r["end_lon"])):
                    if r[which] or lat is None:
                        continue
                    addr = await self.geocoder.reverse(lat, lon)
                    if addr:
                        svc.db.set_geocoded_address(r["id"], which, addr)
                        svc.mark_dirty_trip(r["id"])
            events = svc.db.query(
                "SELECT id, lat, lon FROM consumption_events WHERE address='' AND lat IS NOT NULL ORDER BY id DESC LIMIT ?",
                (GEOCODE_BATCH,),
            )
            for e in events:
                addr = await self.geocoder.reverse(e["lat"], e["lon"])
                if addr:
                    svc.db.set_consumption_address(e["id"], addr)
                    ev = svc.db.get_consumption(e["id"])
                    if ev:
                        svc.mark_dirty_time(ev["start_time"])
            stops = svc.db.query("SELECT id, lat, lon FROM charging_stops WHERE address='' AND lat IS NOT NULL LIMIT ?", (GEOCODE_BATCH,))
            for s in stops:
                addr = await self.geocoder.reverse(s["lat"], s["lon"])
                if addr:
                    svc.db.set_stop_address(s["id"], addr)

    def _publish_mqtt(self) -> None:
        if not self.mqtt:
            return
        from .integrity import summarize

        now = timeutil.now_utc().astimezone(timeutil.local_tz())
        start, end = timeutil.month_bounds(now.year, now.month)
        for vid, svc in self.services.items():
            try:
                last = svc.db.last_trip()
                s = summarize(svc.db.list_trips(start, end))
                status = self.vehicle_status(vid)
                if not self.stream_ok:
                    status += " (CarData-yhteys poikki)"
                self.mqtt.publish_state(vid, "status", status)
                self.mqtt.publish_state(vid, "last_trip_distance", last["distance_km"] if last else None)
                if last:
                    route = f"{last['start_address'] or '?'} → {last['end_address'] or '?'}"
                    self.mqtt.publish_state(vid, "last_trip_route", route)
                self.mqtt.publish_state(vid, "month_business_km", s["business_km"])
                self.mqtt.publish_state(vid, "month_private_km", s["private_all_km"])
                c = svc.ctx.consumption_summary(start, end)
                self.mqtt.publish_state(vid, "month_charged_kwh", c["charged_kwh"])
                if svc.vehicle.has_fuel:
                    self.mqtt.publish_state(vid, "month_refuelled_l", c["refuelled_l"])
                    self.mqtt.publish_state(vid, "month_fuel_used_l", c["fuel_used_l"])
                self.mqtt.publish_state(vid, "last_export", timeutil.fmt_datetime(svc.last_export) if svc.last_export else "")
                self.mqtt.publish_state(vid, "last_import", svc.last_import or "")
            except Exception:
                _LOG.exception("%s: MQTT-julkaisu epäonnistui", vid)
        for vid, errs in self.vehicle_errors.items():
            if vid not in self.services and self.settings.vehicle(vid):
                self.mqtt.publish_state(vid, "status", STATUS_LABELS[NOT_CONFIGURED])

    async def start_mqtt(self) -> None:
        if not self.settings.ha_mqtt_enabled:
            return
        from .ha_mqtt import MqttPublisher, supervisor_mqtt_config

        conf = await supervisor_mqtt_config()
        if not conf:
            return
        try:
            vehicles = [v for v in self.settings.vehicles if v.id in self.services or v.id in self.vehicle_errors]
            self.mqtt = MqttPublisher(conf, vehicles)
        except Exception:
            _LOG.exception("MQTT-yhteyttä ei voitu avata. HA-sensorit jäävät pois.")


async def amain() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOG.info("CarData Logbook käynnistyy. Autot: %s", ", ".join(v.id for v in settings.active_vehicles()) or "ei yhtään")
    app = LogbookApp(settings)
    import uvicorn

    from .web_ui import create_app

    server = uvicorn.Server(
        uvicorn.Config(create_app(app), host="0.0.0.0", port=int(os.environ.get("INGRESS_PORT", "8099")), log_level="warning")
    )
    await app.start_mqtt()
    tasks = [
        asyncio.create_task(app.ha_loop()),
        asyncio.create_task(app.periodic_loop()),
        asyncio.create_task(server.serve()),
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await stop.wait()
    _LOG.info("Sammutetaan: kirjoitetaan odottavat raportit")
    for svc in app.services.values():
        try:
            svc.trip_manager.save()
            if svc.consumption is not None:
                svc.consumption.save()
            svc.write_reports_if_due(force=True)
        except Exception:
            _LOG.exception("Sammutuksen kirjoitus epäonnistui")
    if app.mqtt:
        app.mqtt.stop()
    server.should_exit = True
    for t in tasks:
        t.cancel()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
