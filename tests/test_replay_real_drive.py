"""Todellisen 530e-ajon aikasarja (kellonajat Suomen aikaa), sijainnit siirretty keksittyyn paikkaan.

Ajo A -> kauppa (pysähdys 17:12-17:33) -> koti.
CarData-erityispiirteet tässä datassa:
* liiketilan off tuli noin 5 min todellisen pysähdyksen jälkeen; mittarilukeman
  timestamp-attribuutti kertoo todellisen pysähdyshetken;
* jokaisen pysäköinnin jälkeen: off -> 30 s -> on 7 min -> off ilman liikettä.
"""
from datetime import datetime, timedelta, timezone

from logbook import timeutil
from logbook.trip_manager import DRIVING, IDLE, Event, TripManager, Zone

from helpers import settings

FI = timezone(timedelta(hours=2))
START = (62.2426, 15.7473)
SHOP = (62.4146038889, 15.95137444)
HOME = (62.42626, 15.95672722)


def t(hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 11, 18, h, m, s, tzinfo=FI)


def iso(hms: str) -> str:
    return timeutil.to_iso(t(hms))


def make():
    trips = []
    s = settings()
    tm = TripManager(s.vehicle("530e"), s, on_trip_finished=trips.append, zones=[Zone("zone.home", HOME[0], HOME[1], 100)])
    return tm, trips


def ev(tm, hms, role, state, attrs=None, dup=1):
    for _ in range(dup):
        tm.process(Event(role, str(state), attrs or {}, t(hms)))


def loc(tm, hms, lat, lon):
    ev(tm, hms, "location", "not_home", {"latitude": lat, "longitude": lon}, dup=5)


def odo(tm, hms, value, stamp):
    ev(tm, hms, "odometer", value, {"timestamp": stamp})


EVENTS = [
    ("16:40:00", lambda tm: (loc(tm, "16:40:00", *START), odo(tm, "16:40:00", 28397, "2026-11-18T14:26:19.000Z"),
                             ev(tm, "16:40:00", "motion", "off"), ev(tm, "16:40:00", "charging_state", "NOCHARGING"))),
    ("16:52:21", lambda tm: ev(tm, "16:52:21", "motion", "on")),
    ("16:55:00", lambda tm: loc(tm, "16:55:00", 62.30, 15.82)),
    ("17:03:02", lambda tm: loc(tm, "17:03:02", 62.3487516667, 15.87671472)),
    ("17:05:28", lambda tm: loc(tm, "17:05:28", 62.3737722222, 15.892315)),
    ("17:07:48", lambda tm: loc(tm, "17:07:48", 62.3964638889, 15.91918333)),
    ("17:10:48", lambda tm: loc(tm, "17:10:48", 62.4147283333, 15.94974361)),
    ("17:11:38", lambda tm: loc(tm, "17:11:38", *SHOP)),
    ("17:17:37", lambda tm: ev(tm, "17:17:37", "motion", "off")),
    ("17:17:43", lambda tm: odo(tm, "17:17:43", 28420, "2026-11-18T15:12:30.000Z")),
    ("17:18:07", lambda tm: ev(tm, "17:18:07", "motion", "on")),
    ("17:25:07", lambda tm: ev(tm, "17:25:07", "motion", "off")),
    ("17:34:25", lambda tm: loc(tm, "17:34:25", 62.4145675, 15.95257139)),
    ("17:37:26", lambda tm: (ev(tm, "17:37:26", "motion", "on"), loc(tm, "17:37:26", 62.4250447222, 15.95546278))),
    ("17:38:37", lambda tm: loc(tm, "17:38:37", *HOME)),
    ("17:47:07", lambda tm: ev(tm, "17:47:07", "motion", "off")),
    ("17:47:13", lambda tm: odo(tm, "17:47:13", 28422, "2026-11-18T15:42:00.000Z")),
    ("17:47:37", lambda tm: ev(tm, "17:47:37", "motion", "on")),
    ("17:54:37", lambda tm: ev(tm, "17:54:37", "motion", "off")),
]


def run(periodic_tick: bool):
    tm, trips = make()
    states = {}
    prev = None
    for hms, fn in EVENTS:
        if periodic_tick and prev is not None:
            cur = t(prev) + timedelta(seconds=15)
            while cur < t(hms):
                tm.tick(cur)
                cur += timedelta(seconds=15)
        fn(tm)
        states[hms] = tm.state
        prev = hms
    tm.tick(t("18:30:00"))
    return tm, trips, states


def _check(tm, trips):
    assert tm.state == IDLE
    assert len(trips) == 2, [(x["start_time"], x["end_time"]) for x in trips]
    a, b = trips
    assert a["start_time"] == iso("16:52:21")
    assert a["end_time"] == iso("17:12:30"), "loppu = mittarin oma aikaleima, ei myöhässä tullut off eikä haamuliike"
    assert (a["start_odometer"], a["end_odometer"]) == (28397, 28420)
    assert b["start_time"] == iso("17:34:25"), "alku = ensimmäinen siirtymä pysäköintipaikalta"
    assert b["end_time"] == iso("17:42:00")
    assert (b["start_odometer"], b["end_odometer"]) == (28420, 28422)
    assert abs(b["end_lat"] - HOME[0]) < 1e-9


def test_live_with_periodic_tick():
    tm, trips, states = run(periodic_tick=True)
    _check(tm, trips)
    # kotona: matka päättyy 10 min kuluttua liiketilan off-tilasta, haamuliike ei pidennä sitä
    assert states["17:54:37"] == DRIVING
    assert tm.status_label == "Pysäköity"


def test_backfill_without_periodic_tick():
    """Historiapaikkaus (add-on käynnistyy uudelleen): tick vain tapahtumien kohdalla."""
    tm, trips, _ = run(periodic_tick=False)
    _check(tm, trips)


def test_status_while_waiting_for_trip_end():
    tm, trips = make()
    for hms, fn in EVENTS:
        fn(tm)
        if hms == "17:47:13":
            break
    assert tm.state == DRIVING
    assert tm.status_label == "Pysähtynyt"
