"""Constants for Event Reel."""

from typing import Final

DOMAIN: Final = "event_reel"

# Config entry options
CONF_AI_TASK_ENTITY: Final = "ai_task_entity"
CONF_LANGUAGE: Final = "language"
CONF_RETENTION_DAYS: Final = "retention_days"
CONF_INSTRUCTIONS: Final = "instructions"
CONF_KEEP_NO_ACTIVITY: Final = "keep_no_activity"

# Camera subentries
SUBENTRY_CAMERA: Final = "camera"
CONF_CAMERA: Final = "camera"
CONF_NAME: Final = "name"
CONF_TRIGGERS: Final = "triggers"
CONF_NEST_EVENTS: Final = "nest_events"
CONF_FRAMES: Final = "frames"
CONF_COOLDOWN: Final = "cooldown"

DEFAULT_RETENTION_DAYS: Final = 30
DEFAULT_FRAMES: Final = 3
DEFAULT_COOLDOWN: Final = 90
DEFAULT_NEST_EVENTS: Final = ["person", "chime"]
MAX_FRAMES: Final = 6

# Nest event types (the `type` of a `nest_event`) by the short names used in options.
NEST_EVENT_TYPES: Final = {
    "person": "camera_person",
    "chime": "doorbell_chime",
    "motion": "camera_motion",
    "sound": "camera_sound",
}

# What the AI may name as the main subject. "doorbell" and "motion" are set by triggers.
SUBJECTS: Final = ["person", "vehicle", "animal", "package", "other"]

# Seconds between snapshots when a camera source takes more than one frame.
SNAPSHOT_INTERVAL: Final = 1.0
# Longest edge of a frame sent to the AI, in pixels.
FRAME_MAX_EDGE: Final = 1280
# Waiting jobs before new triggers are dropped (protects a local model).
QUEUE_SIZE: Final = 10
# Seconds one AI answer may take, so a stuck model can't block every later event.
AI_TIMEOUT: Final = 180
# Largest clip or image fetched for an analysis.
MAX_MEDIA_BYTES: Final = 100 * 1024 * 1024

MEDIA_SUBDIR: Final = "event_reel"
STORAGE_KEY: Final = "event_reel.events"
STORAGE_VERSION: Final = 1

# Fired on the event bus when an event is added or updated.
EVENT_EVENT_REEL: Final = "event_reel_event"

ANALYSIS_OK: Final = "ok"
ANALYSIS_FAILED: Final = "failed"
ANALYSIS_NONE: Final = "none"
