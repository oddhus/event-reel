"""The validation library Home Assistant itself uses.

Home Assistant 2026.10 moved from voluptuous to probatio. Schemas handed to HA (config
flows, services, websocket commands) must come from the same library as HA's helpers.
"""

from homeassistant.helpers import config_validation as _cv

vol = getattr(_cv, "probatio", None) or _cv.vol

__all__ = ["vol"]
