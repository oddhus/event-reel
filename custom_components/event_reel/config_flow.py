"""Config flow: one Event Reel entry, its options, and a subentry per camera."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from ._schema import vol
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_CAMERA,
    CONF_COOLDOWN,
    CONF_FRAMES,
    CONF_INSTRUCTIONS,
    CONF_KEEP_NO_ACTIVITY,
    CONF_LANGUAGE,
    CONF_NAME,
    CONF_NEST_EVENTS,
    CONF_RETENTION_DAYS,
    CONF_TRIGGERS,
    DEFAULT_COOLDOWN,
    DEFAULT_FRAMES,
    DEFAULT_NEST_EVENTS,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
    MAX_FRAMES,
    NEST_EVENT_TYPES,
    SUBENTRY_CAMERA,
)

AI_TASK_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="ai_task"))
TEXT = selector.TextSelector()
MULTILINE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def _options_schema(language: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_AI_TASK_ENTITY): AI_TASK_SELECTOR,
            vol.Required(CONF_LANGUAGE, default=language): TEXT,
            vol.Required(
                CONF_RETENTION_DAYS, default=DEFAULT_RETENTION_DAYS
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=365,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )
            ),
            vol.Optional(CONF_INSTRUCTIONS): MULTILINE,
            vol.Required(CONF_KEEP_NO_ACTIVITY, default=False): selector.BooleanSelector(),
        }
    )


def _clean_options(user_input: dict[str, Any]) -> dict[str, Any]:
    options = dict(user_input)
    options[CONF_RETENTION_DAYS] = int(options[CONF_RETENTION_DAYS])
    return {k: v for k, v in options.items() if v not in (None, "")}


class EventReelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Event Reel."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="Event Reel", data={}, options=_clean_options(user_input)
            )
        return self.async_show_form(
            step_id="user", data_schema=_options_schema(self.hass.config.language)
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EventReelOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_CAMERA: CameraSubentryFlow}


class EventReelOptionsFlow(OptionsFlow):
    """Change the AI, the language and how long events are kept."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean_options(user_input))
        schema = self.add_suggested_values_to_schema(
            _options_schema(self.hass.config.language), self.config_entry.options
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _camera_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CAMERA): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="camera")
            ),
            vol.Optional(CONF_NAME): TEXT,
            vol.Optional(CONF_TRIGGERS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "event", "input_boolean"], multiple=True
                )
            ),
            vol.Optional(CONF_NEST_EVENTS, default=DEFAULT_NEST_EVENTS): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(NEST_EVENT_TYPES),
                    multiple=True,
                    translation_key=CONF_NEST_EVENTS,
                )
            ),
            vol.Required(CONF_FRAMES, default=DEFAULT_FRAMES): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=MAX_FRAMES, step=1)
            ),
            vol.Required(CONF_COOLDOWN, default=DEFAULT_COOLDOWN): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Optional(CONF_INSTRUCTIONS): MULTILINE,
        }
    )


def _clean_camera(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_FRAMES] = int(data[CONF_FRAMES])
    data[CONF_COOLDOWN] = int(data[CONF_COOLDOWN])
    return {k: v for k, v in data.items() if v not in (None, "")}


class CameraSubentryFlow(ConfigSubentryFlow):
    """Add or change a camera to describe."""

    def _title(self, data: dict[str, Any]) -> str:
        if data.get(CONF_NAME):
            return data[CONF_NAME]
        state = self.hass.states.get(data[CONF_CAMERA])
        return (
            str(state.attributes.get("friendly_name", data[CONF_CAMERA]))
            if state
            else data[CONF_CAMERA]
        )

    def _taken(self, camera: str, except_id: str | None = None) -> bool:
        return any(
            sub.data.get(CONF_CAMERA) == camera and sub.subentry_id != except_id
            for sub in self._get_entry().subentries.values()
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._taken(user_input[CONF_CAMERA]):
                errors[CONF_CAMERA] = "already_configured"
            else:
                data = _clean_camera(user_input)
                return self.async_create_entry(
                    title=self._title(data), data=data, unique_id=data[CONF_CAMERA]
                )
        schema = _camera_schema()
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._taken(user_input[CONF_CAMERA], subentry.subentry_id):
                errors[CONF_CAMERA] = "already_configured"
            else:
                data = _clean_camera(user_input)
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    data=data,
                    title=self._title(data),
                    unique_id=data[CONF_CAMERA],
                )
        schema = self.add_suggested_values_to_schema(
            _camera_schema(), user_input or dict(subentry.data)
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)
