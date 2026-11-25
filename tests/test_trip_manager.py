from helpers import CHARGER, HOME, T0, WORK, Sim, lerp

from logbook import timeutil
from logbook.trip_manager import DRIVING, IDLE, PAUSED_CHARGING


def parked_at_home(sim, odo=10000):
    sim.at(0).loc(*HOME).ev("odometer", odo).ev("motion", "off").ev("charging_state", "NOCHARGING").ev("soc", 80)
    return sim


def drive(sim, start_s, frm, to, minutes, odo_start, km):
    """Ajo: motion on, sijainti 2 min välein (5 kopiota), motion off, mittari 5 s myöhemmin."""
    sim.at(start_s).ev("motion", "on")
    steps = max(1, minutes // 2)
    for i in range(1, steps + 1):
        sim.at(start_s + i * 120 - 10).loc(*lerp(frm, to, i / steps))
        if i * 120 >= 1800 and i % 15 == 0:
            sim.ev("odometer", odo_start + round(km * i / steps))
    end_s = start_s + minutes * 60
    sim.at(end_s).ev("motion", "off")
    sim.at(end_s + 5).ev("odometer", odo_start + km)
    return end_s


def test_normal_trip_with_duplicates_and_ghost():
    sim = parked_at_home(Sim())
    end_s = drive(sim, 600, HOME, WORK, 20, 10000, 15)
    # haamuliike pysäköinnin jälkeen: off -> on 30 s -> off, ei siirtymää
    sim.at(end_s + 60).ev("motion", "on")
    sim.at(end_s + 90).ev("motion", "off")
    assert sim.tm.state == DRIVING
    sim.tick(end_s + 590)
    assert sim.trips == []
    sim.tick(end_s + 601)
    assert len(sim.trips) == 1
    trip = sim.trips[0]
    assert trip["start_time"] == timeutil.to_iso(sim.at(600).t)
    assert trip["end_time"] == timeutil.to_iso(sim.at(end_s).t), "loppuaika = todellinen pysähdys, ei haamuliike"
    assert trip["start_odometer"] == 10000
    assert trip["end_odometer"] == 10015
    assert trip["distance_km"] == 15
    assert (trip["start_lat"], trip["start_lon"]) == HOME
    assert abs(trip["end_lat"] - WORK[0]) < 1e-9
    assert sim.tm.ignored_ghosts >= 1
    assert sim.tm.ignored_duplicates >= 4 * 10
    assert sim.tm.state == IDLE


def test_ghost_motion_while_parked_creates_no_trip():
    sim = parked_at_home(Sim())
    for k in range(5):
        base = 3600 * (k + 1)
        sim.at(base).ev("motion", "on")
        sim.at(base + 30).ev("motion", "off")
        sim.at(base + 31).loc(*HOME)  # sama sijainti uudelleen
    sim.tick(30000)
    assert sim.trips == []
    assert sim.tm.state == IDLE
    assert sim.tm.ignored_ghosts == 5


def test_short_stop_splits_trips_and_odometer_continuity():
    sim = parked_at_home(Sim())
    e1 = drive(sim, 600, HOME, WORK, 10, 10000, 8)
    sim.tick(e1 + 700)
    e2 = drive(sim, e1 + 800, WORK, HOME, 10, 10008, 8)
    sim.tick(e2 + 700)
    assert len(sim.trips) == 2
    assert sim.trips[0]["end_odometer"] == sim.trips[1]["start_odometer"] == 10008
    assert (sim.trips[1]["start_lat"], sim.trips[1]["start_lon"]) == (sim.trips[0]["end_lat"], sim.trips[0]["end_lon"])


def test_charging_pause_keeps_trip_together():
    sim = parked_at_home(Sim(vehicle_id="ix3"))
    e1 = drive(sim, 600, HOME, CHARGER, 120, 10000, 180)
    sim.at(e1 + 60).ev("charging_method", "DC").ev("charging_state", "CHARGING")
    sim.tick(e1 + 200)
    assert sim.tm.state == PAUSED_CHARGING
    sim.tick(e1 + 2400)  # 40 min latausta, yli tavallisen 10 min aikakatkaisun
    assert sim.trips == []
    sim.at(e1 + 2500).ev("soc", 80).ev("charging_state", "NOCHARGING")
    e2 = drive(sim, e1 + 2600, CHARGER, (62.6, 26.0), 30, 10180, 40)
    assert sim.tm.state == DRIVING
    sim.tick(e2 + 700)
    assert len(sim.trips) == 1
    trip = sim.trips[0]
    assert trip["distance_km"] == 220
    assert len(trip["charging_stops"]) == 1
    stop = trip["charging_stops"][0]
    assert stop["method"] == "DC"
    assert stop["start_time"] == timeutil.to_iso(sim.at(e1 + 60).t)


def test_charging_pause_timeout_ends_trip_at_last_movement():
    sim = parked_at_home(Sim())
    e1 = drive(sim, 600, HOME, CHARGER, 60, 10000, 90)
    sim.at(e1 + 30).ev("charging_state", "CHARGING")
    sim.tick(e1 + 2000)
    assert sim.trips == [] and sim.tm.state == PAUSED_CHARGING
    sim.tick(e1 + 2701)
    assert len(sim.trips) == 1
    assert sim.trips[0]["end_time"] == timeutil.to_iso(sim.at(e1).t)
    assert sim.trips[0]["charging_stops"] == []


def test_home_charging_is_not_a_pause():
    sim = parked_at_home(Sim())
    e1 = drive(sim, 600, HOME, WORK, 10, 10000, 8)
    sim.tick(e1 + 700)
    e2 = drive(sim, e1 + 800, WORK, HOME, 10, 10008, 8)
    sim.at(e2 + 30).ev("charging_state", "CHARGING")
    sim.tick(e2 + 601)
    assert len(sim.trips) == 2
    assert sim.tm.state == IDLE


def test_stream_disconnect_does_not_end_trip():
    sim = parked_at_home(Sim())
    sim.at(600).ev("motion", "on")
    sim.at(720).loc(*WORK)
    sim.tm.stream_ok = False
    sim.at(900).ev("motion", "off")
    sim.tick(5000)
    assert sim.trips == [] and sim.tm.state == DRIVING
    sim.tm.stream_ok = True
    sim.tick(5001)
    assert len(sim.trips) == 1
    assert sim.trips[0]["end_time"] == timeutil.to_iso(sim.at(900).t)


def test_without_motion_entity_uses_location_and_timeout():
    sim = parked_at_home(Sim(motion=False))
    for i in range(1, 6):
        sim.at(600 + i * 120).loc(*lerp(HOME, WORK, i / 5))
    sim.at(600 + 5 * 120 + 5).ev("odometer", 10008)
    sim.tick(600 + 5 * 120 + 300)
    assert sim.trips == []
    sim.tick(600 + 5 * 120 + 610)
    assert len(sim.trips) == 1
    assert sim.trips[0]["distance_km"] == 8


def test_state_survives_restart(tmp_path):
    path = tmp_path / "active_trip.json"
    sim = parked_at_home(Sim(state_path=path))
    sim.at(600).ev("motion", "on")
    sim.at(720).loc(*lerp(HOME, WORK, 0.5))
    assert sim.tm.state == DRIVING
    sim2 = Sim(state_path=path)
    assert sim2.tm.state == DRIVING
    sim2.at(900).loc(*WORK)
    sim2.at(1000).ev("motion", "off")
    sim2.at(1005).ev("odometer", 10007)
    sim2.tick(1700)
    assert len(sim2.trips) == 1
    assert sim2.trips[0]["start_time"] == timeutil.to_iso(T0.replace(minute=10))
    assert sim2.trips[0]["distance_km"] == 7


def test_unavailable_states_are_ignored():
    sim = parked_at_home(Sim())
    sim.at(100).ev("odometer", "unavailable").ev("motion", "unknown")
    sim.at(150).loc(None, None)
    assert sim.tm.s.odometer == 10000
    assert sim.tm.state == IDLE


def test_charging_at_trip_start_ends_trip_without_home_zone():
    sim = parked_at_home(Sim(zones=[]))
    e1 = drive(sim, 600, HOME, WORK, 10, 10000, 8)
    e2 = drive(sim, e1 + 120, WORK, HOME, 10, 10008, 8)  # käynti kääntymässä, takaisin lähtöpaikalle
    sim.at(e2 + 30).ev("charging_method", "AC").ev("charging_state", "CHARGINGACTIVE")
    sim.tick(e2 + 200)
    assert sim.tm.state == DRIVING
    sim.tick(e2 + 601)
    assert sim.tm.state == IDLE and len(sim.trips) == 1
