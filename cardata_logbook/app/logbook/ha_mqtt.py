"""Autokohtaiset HA-sensorit MQTT Discoveryllä (vain uusia bmw_logbook_-entiteettejä)."""
from __future__ import annotations

import json
import logging
import os

_LOG = logging.getLogger(__name__)

AVAIL_TOPIC = "bmw_logbook/status"

SENSORS = {
    "status": {"name": "Tila", "icon": "mdi:car-info"},
    "last_trip_distance": {"name": "Viimeisin matka", "unit_of_measurement": "km", "icon": "mdi:map-marker-distance", "state_class": "measurement"},
    "last_trip_route": {"name": "Viimeisin reitti", "icon": "mdi:routes"},
    "month_business_km": {"name": "Työajot tässä kuussa", "unit_of_measurement": "km", "icon": "mdi:briefcase"},
    "month_private_km": {"name": "Yksityisajot tässä kuussa", "unit_of_measurement": "km", "icon": "mdi:home"},
    "month_charged_kwh": {"name": "Ladattu tässä kuussa", "unit_of_measurement": "kWh", "icon": "mdi:ev-station"},
    "month_refuelled_l": {"name": "Tankattu tässä kuussa", "unit_of_measurement": "L", "icon": "mdi:gas-station"},
    "month_fuel_used_l": {"name": "Polttoainetta kulunut tässä kuussa", "unit_of_measurement": "L", "icon": "mdi:fuel"},
    "last_export": {"name": "Raportit kirjoitettu", "icon": "mdi:file-export"},
    "last_import": {"name": "Viimeisin Excel-tuonti", "icon": "mdi:file-import"},
}

FUEL_SENSORS = ("month_refuelled_l", "month_fuel_used_l")


async def supervisor_mqtt_config() -> dict | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    _LOG.warning("MQTT-palvelua ei löytynyt Supervisorilta (%s). HA-sensorit jäävät pois.", r.status)
                    return None
                return (await r.json()).get("data")
    except Exception as exc:
        _LOG.warning("MQTT-asetuksia ei saatu: %s", exc)
        return None


class MqttPublisher:
    def __init__(self, conf: dict, vehicles: list):
        import paho.mqtt.client as mqtt

        self.vehicles = vehicles
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bmw_logbook")
        if conf.get("username"):
            self.client.username_pw_set(conf["username"], conf.get("password"))
        if conf.get("ssl"):
            self.client.tls_set()
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.connect_async(conf.get("host", "core-mosquitto"), int(conf.get("port", 1883)))
        self.client.loop_start()
        self._last: dict[str, str] = {}

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        _LOG.info("MQTT yhdistetty (%s)", reason_code)
        client.publish(AVAIL_TOPIC, "online", retain=True)
        for v in self.vehicles:
            self.publish_discovery(v)
        for topic, payload in self._last.items():
            client.publish(topic, payload, retain=True)

    def publish_discovery(self, vehicle) -> None:
        device = {
            "identifiers": [f"bmw_logbook_{vehicle.id}"],
            "name": f"CarData Logbook {vehicle.id}",
            "manufacturer": "CarData Logbook",
            "model": vehicle.display_name,
        }
        for key, meta in SENSORS.items():
            if key in FUEL_SENSORS and not getattr(vehicle, "has_fuel", True):
                continue
            uid = f"bmw_logbook_{vehicle.id}_{key}"
            payload = {
                **meta,
                "unique_id": uid,
                "default_entity_id": f"sensor.{uid}",
                "state_topic": f"bmw_logbook/{vehicle.id}/{key}",
                "availability_topic": AVAIL_TOPIC,
                "device": device,
            }
            self.client.publish(f"homeassistant/sensor/{uid}/config", json.dumps(payload, ensure_ascii=False), retain=True)

    def publish_state(self, vehicle_id: str, key: str, value) -> None:
        topic = f"bmw_logbook/{vehicle_id}/{key}"
        payload = "" if value is None else str(value)[:255]
        if self._last.get(topic) == payload:
            return
        self._last[topic] = payload
        self.client.publish(topic, payload, retain=True)

    def stop(self) -> None:
        self.client.publish(AVAIL_TOPIC, "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()
