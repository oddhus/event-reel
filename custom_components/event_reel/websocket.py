"""Websocket commands for the Event Reel card."""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from ._schema import vol
from .const import DOMAIN
from .store import select_events

SIGNAL_EVENT = f"{DOMAIN}_event"


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list_events)
    websocket_api.async_register_command(hass, ws_subscribe_events)


def _store(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data.store
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "event_reel/events/list",
        vol.Optional("start_time"): str,
        vol.Optional("end_time"): str,
        vol.Optional("cameras"): [str],
        vol.Optional("limit"): vol.All(int, vol.Range(min=1, max=5000)),
    }
)
@callback
def ws_list_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Events in a time range, newest first."""
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Event Reel is not loaded")
        return
    bounds = {}
    for key in ("start_time", "end_time"):
        if key in msg:
            parsed = dt_util.parse_datetime(msg[key])
            if parsed is None:
                connection.send_error(msg["id"], "invalid_format", f"Invalid {key}")
                return
            bounds[key] = parsed
    events = select_events(
        store.all(),
        start=bounds.get("start_time"),
        end=bounds.get("end_time"),
        cameras=msg.get("cameras"),
        limit=msg.get("limit"),
    )
    connection.send_result(msg["id"], {"events": events})


@websocket_api.websocket_command({vol.Required("type"): "event_reel/events/subscribe"})
@callback
def ws_subscribe_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Push every added, updated or removed event: {"action": ..., "event": {...}}."""

    @callback
    def forward(action: str, event: dict[str, Any]) -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"action": action, "event": event})
        )

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_EVENT, forward)
    connection.send_result(msg["id"])
