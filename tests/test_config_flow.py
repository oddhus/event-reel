"""Setting up Event Reel and adding cameras."""

from __future__ import annotations

from pathlib import Path

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.event_reel.const import DOMAIN, SUBENTRY_CAMERA

from .conftest import SetUp, camera_subentry


async def test_user_step_creates_entry_with_options(hass: HomeAssistant, media_dir: Path) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"language": "nb", "retention_days": 14.0, "keep_no_activity": False, "instructions": ""},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Event Reel"
    # Empty text is dropped and the number becomes an int.
    assert result["options"] == {"language": "nb", "retention_days": 14, "keep_no_activity": False}


async def test_only_one_entry(hass: HomeAssistant, set_up: SetUp) -> None:
    await set_up()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_changes_language(hass: HomeAssistant, set_up: SetUp) -> None:
    entry = await set_up(options={"retention_days": 30})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"language": "de", "retention_days": 7, "keep_no_activity": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"language": "de", "retention_days": 7, "keep_no_activity": True}


async def test_add_camera_subentry(hass: HomeAssistant, set_up: SetUp) -> None:
    hass.states.async_set("camera.garden", "idle", {"friendly_name": "Garden"})
    entry = await set_up()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CAMERA), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "camera": "camera.garden",
            "triggers": ["binary_sensor.garden_person"],
            "nest_events": [],
            "frames": 2.0,
            "cooldown": 60.0,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Garden"
    (subentry,) = entry.subentries.values()
    assert subentry.unique_id == "camera.garden"
    assert subentry.data == {
        "camera": "camera.garden",
        "triggers": ["binary_sensor.garden_person"],
        "nest_events": [],
        "frames": 2,
        "cooldown": 60,
    }
    # Adding a camera reloads the entry, which starts a source for it.
    await hass.async_block_till_done()
    assert [s.camera for s in entry.runtime_data.sources] == ["camera.garden"]


async def test_same_camera_twice_is_refused(hass: HomeAssistant, set_up: SetUp) -> None:
    entry = await set_up(cameras=[camera_subentry("camera.garden")])

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CAMERA), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"camera": "camera.garden", "frames": 3, "cooldown": 90}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"camera": "already_configured"}
