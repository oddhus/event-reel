"""The validation library Home Assistant itself uses.

Home Assistant 2026.10 moved from voluptuous to probatio. Schemas handed to HA (config
flows, services, websocket commands) must come from the same library as HA's helpers.
"""

from typing import Any

from homeassistant.helpers import config_validation as _cv

# Any to a type checker: which library it is depends on the Home Assistant version.
vol: Any = getattr(_cv, "probatio", None) or _cv.vol  # type: ignore[attr-defined]

__all__ = ["vol"]
