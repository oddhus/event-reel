"""Event Reel: AI descriptions of camera events, for the Event Reel card."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import shutil
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .analyzer import Analyzer, Settings
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_INSTRUCTIONS,
    CONF_KEEP_NO_ACTIVITY,
    CONF_LANGUAGE,
    CONF_RETENTION_DAYS,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
    EVENT_EVENT_REEL,
    SUBENTRY_CAMERA,
)
from .runtime import EventReelData
from .services import async_setup_services
from .sources import CameraSource
from .store import EventStore, MediaFolder
from .websocket import SIGNAL_EVENT, async_setup_websocket

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PURGE_INTERVAL = timedelta(hours=6)

type EventReelConfigEntry = ConfigEntry[EventReelData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    async_setup_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EventReelConfigEntry) -> bool:
    media = MediaFolder(hass)
    # Frames of an analysis that was cut short by a restart.
    await hass.async_add_executor_job(shutil.rmtree, media.path_for("_work"), True)

    store = EventStore(hass, media)
    await store.async_load()

    options = entry.options
    analyzer = Analyzer(
        hass,
        store,
        Settings(
            ai_task_entity=options.get(CONF_AI_TASK_ENTITY) or None,
            language=options.get(CONF_LANGUAGE) or hass.config.language,
            instructions=options.get(CONF_INSTRUCTIONS) or None,
            keep_no_activity=options.get(CONF_KEEP_NO_ACTIVITY, False),
        ),
    )
    data = EventReelData(store=store, analyzer=analyzer)
    entry.runtime_data = data

    @callback
    def relay(action: str, event: dict[str, Any]) -> None:
        async_dispatcher_send(hass, SIGNAL_EVENT, action, event)
        hass.bus.async_fire(EVENT_EVENT_REEL, {"action": action, "event": event})

    entry.async_on_unload(store.async_listen(relay))

    retention = timedelta(days=options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS))

    async def purge(_now: datetime | None = None) -> None:
        if removed := await store.async_purge(dt_util.utcnow() - retention):
            _LOGGER.debug("Removed %s events older than %s", removed, retention)

    await purge()
    entry.async_on_unload(async_track_time_interval(hass, purge, PURGE_INTERVAL))

    analyzer.async_start()
    data.sources = [
        CameraSource(hass, analyzer, subentry.data)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_CAMERA
    ]

    # Nest devices and trigger entities are registered by the time HA has started.
    @callback
    def start_sources(_hass: HomeAssistant) -> None:
        for source in data.sources:
            source.async_start()

    entry.async_on_unload(async_at_started(hass, start_sources))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EventReelConfigEntry) -> bool:
    data = entry.runtime_data
    for source in data.sources:
        source.async_stop()
    await data.analyzer.async_stop()
    await data.store.async_flush()
    return True


async def _async_reload(hass: HomeAssistant, entry: EventReelConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
