"""Home Assistantin WebSocket-asiakas (vain luku).

Käyttää Supervisorin antamaa SUPERVISOR_TOKEN-tunnusta. Palvelu ei koskaan kutsu HA:n
palveluita eikä muuta entiteettejä: käytössä ovat vain get_states, config/entity_registry/list,
subscribe_entities ja history/history_during_period.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from . import timeutil

_LOG = logging.getLogger(__name__)

StateCallback = Callable[[str, dict], Awaitable[None] | None]


def ws_url() -> str:
    return os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")


def ws_token() -> str:
    return os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")


def expand_compressed(entity_id: str, comp: dict, previous: dict | None = None) -> dict:
    """Muuntaa subscribe_entities/historian tiiviin muodon tavalliseksi tilaobjektiksi."""
    lc = comp.get("lc")
    lu = comp.get("lu", lc)
    return {
        "entity_id": entity_id,
        "state": comp.get("s", (previous or {}).get("state")),
        "attributes": dict(comp.get("a") or {}),
        "last_changed": lc if lc is not None else (previous or {}).get("last_changed"),
        "last_updated": lu if lu is not None else (lc if lc is not None else (previous or {}).get("last_updated")),
    }


def apply_diff(current: dict, diff: dict) -> dict:
    new = dict(current)
    new["attributes"] = dict(current.get("attributes") or {})
    plus = diff.get("+") or {}
    if "s" in plus:
        new["state"] = plus["s"]
    if "a" in plus:
        new["attributes"].update(plus["a"])
    if "lc" in plus:
        new["last_changed"] = plus["lc"]
        new["last_updated"] = plus["lc"]
    if "lu" in plus:
        new["last_updated"] = plus["lu"]
    minus = diff.get("-") or {}
    for key in minus.get("a") or []:
        new["attributes"].pop(key, None)
    return new


class HAClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = url or ws_url()
        self.token = token if token is not None else ws_token()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[int, Callable[[dict], Any]] = {}
        self._ws = None
        self._session = None
        self._reader: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.states: dict[str, dict] = {}

    async def connect(self) -> None:
        import aiohttp

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.url, heartbeat=30, max_msg_size=64 * 1024 * 1024)
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_required":
            raise ConnectionError(f"Odottamaton vastaus: {msg}")
        await self._ws.send_json({"type": "auth", "access_token": self.token})
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_ok":
            raise ConnectionError("HA:n tunnistautuminen epäonnistui")
        self.closed.clear()
        self._reader = asyncio.create_task(self._read_loop())
        self.connected.set()
        _LOG.info("Yhdistetty Home Assistantiin (%s)", self.url)

    async def close(self) -> None:
        self.connected.clear()
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        if self._reader is not None:
            self._reader.cancel()

    async def _read_loop(self) -> None:
        import aiohttp

        try:
            async for raw in self._ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                data = json.loads(raw.data)
                for msg in data if isinstance(data, list) else [data]:
                    await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("HA WebSocket -lukuvirhe")
        finally:
            self.connected.clear()
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Yhteys katkesi"))
            self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if msg.get("type") == "result" and mid in self._pending:
            fut = self._pending.pop(mid)
            if msg.get("success"):
                fut.set_result(msg.get("result"))
            else:
                fut.set_exception(RuntimeError(str(msg.get("error"))))
        elif msg.get("type") == "event" and mid in self._subs:
            res = self._subs[mid](msg.get("event") or {})
            if asyncio.iscoroutine(res):
                await res

    async def call(self, payload: dict, timeout: float = 60) -> Any:
        if self._ws is None:
            raise ConnectionError("Ei yhteyttä")
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json({"id": mid, **payload})
        return await asyncio.wait_for(fut, timeout)

    async def get_states(self) -> list[dict]:
        states = await self.call({"type": "get_states"})
        for s in states:
            self.states[s["entity_id"]] = s
        return states

    async def entity_registry(self) -> list[dict]:
        """HA:n entiteettirekisteri (unique_id ja platform autojen löytämiseen)."""
        return await self.call({"type": "config/entity_registry/list"}) or []

    async def history(self, entity_ids: list[str], start: datetime, end: datetime | None = None) -> dict[str, list[dict]]:
        payload = {
            "type": "history/history_during_period",
            "start_time": start.isoformat(),
            "entity_ids": entity_ids,
            "minimal_response": False,
            "no_attributes": False,
            "significant_changes_only": False,
            "include_start_time_state": True,
        }
        if end is not None:
            payload["end_time"] = end.isoformat()
        result = await self.call(payload, timeout=180) or {}
        out: dict[str, list[dict]] = {}
        for eid, rows in result.items():
            prev = None
            items = []
            for row in rows:
                if "s" in row or "lu" in row:
                    st = expand_compressed(eid, row, prev)
                else:
                    st = row
                items.append(st)
                prev = st
            out[eid] = items
        return out

    async def subscribe_entities(self, entity_ids: list[str], callback: StateCallback) -> None:
        """Kutsuu callback(entity_id, tilaobjekti) jokaisesta muutoksesta."""

        async def on_event(event: dict) -> None:
            for eid, comp in (event.get("a") or {}).items():
                st = expand_compressed(eid, comp)
                self.states[eid] = st
                res = callback(eid, st)
                if asyncio.iscoroutine(res):
                    await res
            for eid, diff in (event.get("c") or {}).items():
                current = self.states.get(eid) or {"entity_id": eid, "attributes": {}}
                st = apply_diff(current, diff)
                self.states[eid] = st
                res = callback(eid, st)
                if asyncio.iscoroutine(res):
                    await res

        mid = next(self._ids)
        self._subs[mid] = on_event
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json({"id": mid, "type": "subscribe_entities", "entity_ids": entity_ids})
        await asyncio.wait_for(fut, 30)


def state_timestamp(state: dict) -> datetime:
    ts = state.get("last_updated") or state.get("last_changed")
    if isinstance(ts, (int, float)):
        return timeutil.from_ts(ts)
    if ts:
        return timeutil.parse_iso(ts)
    return timeutil.now_utc()
