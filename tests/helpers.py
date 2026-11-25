from datetime import datetime, timedelta, timezone

from logbook.trip_manager import Event, TripManager, Zone
from logbook.vehicle_registry import parse_settings

T0 = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)

HOME = (61.4978, 23.7610)
WORK = (61.4500, 23.8500)
CHARGER = (62.2415, 25.7209)


def settings(**over):
    opts = {
        "vehicles": [
            {
                "id": "530e",
                "plate": "ABC-123",
                "make": "BMW",
                "model": "530e xDrive (G30)",
                "powertrain": "PHEV",
                "vin": "",
                "driver": "Kuljettaja",
                "enabled": True,
                "charging_pause_enabled": True,
                "charging_pause_timeout_s": 2700,
                "entity_location": "device_tracker.530e_xdrive_location",
                "entity_odometer": "sensor.530e_xdrive_vehicle_mileage",
                "entity_motion": "binary_sensor.autot_530e_xdrive_vehicle_motion_state",
                "entity_soc": "sensor.530e_xdrive_battery_ev_state_of_charge",
                "entity_charging_state": "sensor.charging_ev_charging_state",
                "entity_charging_method": "sensor.charging_ev_charging_method",
            },
            {
                "id": "ix3",
                "plate": "XYZ-789",
                "make": "BMW",
                "model": "iX3 (NA5)",
                "powertrain": "BEV",
                "vin": "",
                "driver": "Kuljettaja",
                "enabled": True,
                "charging_pause_enabled": True,
                "charging_pause_timeout_s": 5400,
                "entity_location": "device_tracker.ix3_location",
                "entity_odometer": "sensor.ix3_mileage",
                "entity_motion": "binary_sensor.ix3_motion",
                "entity_charging_state": "sensor.charging_ev_charging_state_2",
                "entity_charging_method": "sensor.charging_ev_charging_method_2",
            },
        ],
    }
    opts.update(over)
    return parse_settings(opts, data_dir="/tmp")


class Sim:
    """Syöttää tilakoneelle CarDatan kaltaista dataa."""

    def __init__(self, vehicle_id="530e", motion=True, zones=None, state_path=None, s=None):
        self.settings = s or settings()
        v = self.settings.vehicle(vehicle_id)
        if not motion:
            v.entities["motion"] = ""
        self.trips = []
        self.tm = TripManager(
            v,
            self.settings,
            on_trip_finished=self.trips.append,
            state_path=state_path,
            zones=zones if zones is not None else [Zone("zone.home", HOME[0], HOME[1], 100)],
        )
        self.t = T0

    def at(self, seconds):
        self.t = T0 + timedelta(seconds=seconds)
        return self

    def ev(self, role, state, attrs=None, dup=1):
        for _ in range(dup):
            self.tm.process(Event(role, str(state), attrs or {}, self.t))
        return self

    def loc(self, lat, lon, dup=5):
        return self.ev("location", "not_home", {"latitude": lat, "longitude": lon}, dup=dup)

    def tick(self, seconds):
        self.t = T0 + timedelta(seconds=seconds)
        self.tm.tick(self.t)
        return self


def lerp(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
