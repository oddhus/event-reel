"""Camera sources: when to describe a camera, and where its frames come from."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import logging
import re
from typing import Any, Final

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .analyzer import Analyzer, Job
from .const import (
    CONF_CAMERA,
    CONF_COOLDOWN,
    CONF_FRAMES,
    CONF_INSTRUCTIONS,
    CONF_NAME,
    CONF_NEST_EVENTS,
    CONF_TRIGGERS,
    DEFAULT_COOLDOWN,
    DEFAULT_FRAMES,
    DEFAULT_NEST_EVENTS,
    NEST_EVENT_TYPES,
)

_LOGGER = logging.getLogger(__name__)

NEST_EVENT = "nest_event"
NEST_DOMAIN = "nest"
LABEL_BY_NEST_TYPE = {nest_type: label for label, nest_type in NEST_EVENT_TYPES.items()}
# `EventDeviceClass.DOORBELL`. Importing the event integration would make it a dependency.
DOORBELL_DEVICE_CLASS = "doorbell"


def nest_device_id(hass: HomeAssistant, camera: str) -> str | None:
    """The HA device id of a Nest camera entity, or None for any other camera."""
    entry = er.async_get(hass).async_get(camera)
    if entry is None or entry.device_id is None:
        return None
    device = dr.async_get(hass).async_get(entry.device_id)
    if device is None or not any(domain == NEST_DOMAIN for domain, _ in device.identifiers):
        return None
    return device.id


def is_trigger(old_state: str | None, new_state: str | None, domain: str) -> bool:
    """Whether a state change of a trigger entity should start an analysis."""
    if new_state is None or new_state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return False
    if domain == "event":
        # An event entity's state is the time of its last event: any new time is an event,
        # except the first state after startup or after it was unavailable. "unknown" means
        # it never fired, so its first event counts.
        return old_state not in (None, STATE_UNAVAILABLE) and new_state != old_state
    return new_state == STATE_ON and old_state != STATE_ON


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and (parsed := dt_util.parse_datetime(value)):
        return parsed
    return dt_util.utcnow()


class CameraSource:
    """One configured camera: Nest events for Nest cameras, and optional trigger entities."""

    def __init__(self, hass: HomeAssistant, analyzer: Analyzer, data: Mapping[str, Any]) -> None:
        self.hass = hass
        self.analyzer = analyzer
        self.camera: str = data[CONF_CAMERA]
        self.name: str = data.get(CONF_NAME) or self._friendly_name()
        self.triggers: list[str] = list(data.get(CONF_TRIGGERS) or [])
        self.nest_events: list[str] = list(data.get(CONF_NEST_EVENTS, DEFAULT_NEST_EVENTS))
        self.frames = int(data.get(CONF_FRAMES, DEFAULT_FRAMES))
        self.cooldown = float(data.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))
        self.instructions: str | None = data.get(CONF_INSTRUCTIONS) or None
        self.nest_device: str | None = None
        self._unsubs: list[Callable[[], None]] = []

    def _friendly_name(self) -> str:
        state = self.hass.states.get(self.camera)
        if state and (name := state.attributes.get("friendly_name")):
            return str(name)
        return self.camera.split(".", 1)[-1].replace("_", " ").capitalize()

    @callback
    def async_start(self) -> None:
        self.nest_device = nest_device_id(self.hass, self.camera)
        if self.nest_device and self.nest_events:
            self._unsubs.append(self.hass.bus.async_listen(NEST_EVENT, self._on_nest_event))
        if self.triggers:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.triggers, self._on_trigger)
            )

    @property
    def listening(self) -> bool:
        """Whether it listens for Nest events or trigger entities."""
        return bool(self._unsubs)

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def _job(self, **kwargs: Any) -> Job:
        return Job(
            camera=self.camera,
            camera_name=self.name,
            frames=self.frames,
            instructions=self.instructions,
            **kwargs,
        )

    @callback
    def _on_nest_event(self, event: Event) -> None:
        data = event.data
        if data.get("device_id") != self.nest_device:
            return
        label = LABEL_BY_NEST_TYPE.get(data.get("type", ""))
        if label is None or label not in self.nest_events:
            return
        attachment = data.get("attachment") or {}
        token = data.get("nest_event_id")
        if not token:
            return
        video = attachment.get("video")
        # Cameras without clips send only an image. The attachment is its 175 px thumbnail;
        # the same path without `/thumbnail` is the full picture.
        image = None if video else attachment.get("image")
        if image:
            image = image.removesuffix("/thumbnail")
        job = self._job(
            start=_parse_time(data.get("timestamp")),
            source="nest",
            labels=[label],
            video=video,
            image=image,
            nest={"device_id": self.nest_device, "event_token": token, "video": bool(video)},
            keep_without_activity=label == "chime",
        )
        self.analyzer.async_submit(job, self.cooldown)

    @callback
    def _on_trigger(self, event: Event[EventStateChangedData]) -> None:
        old = event.data["old_state"]
        new = event.data["new_state"]
        entity_id = event.data["entity_id"]
        if not is_trigger(
            old.state if old else None, new.state if new else None, entity_id.split(".")[0]
        ):
            return
        label = self._trigger_label(entity_id)
        job = self._job(
            start=dt_util.utcnow(),
            source="trigger",
            labels=[label],
            snapshot=True,
            trigger=entity_id,
            keep_without_activity=label == "chime",
        )
        self.analyzer.async_submit(job, self.cooldown)

    def _trigger_label(self, entity_id: str) -> str:
        """What the trigger's detector looks for, from its entity id, name or device class,
        or 'chime' for a doorbell's event entity."""
        state = self.hass.states.get(entity_id)
        device_class = state.attributes.get("device_class") if state else None
        # A doorbell's event entity, such as one from Ring or UniFi Protect, is a ring like a
        # Nest chime. Binary sensors have no doorbell device class, so they can't be told apart.
        if entity_id.startswith("event.") and device_class == DOORBELL_DEVICE_CLASS:
            return "chime"
        # A sound sensor hears: Frigate's `<camera>_dog_sound` is a bark, not a dog in view.
        if device_class == "sound":
            return "sound"
        # A sensor that sees or smells doesn't hear: Dahua's `<camera>_motion_alarm` is a
        # motion sensor, and a `kitchen_smoke_alarm` with the smoke class smells smoke. Others
        # may, such as UniFi Protect's audio sensors, whose `co_alarm_detected` has the carbon
        # monoxide device class and the rest none.
        hears = device_class not in DEAF_CLASSES
        name = state.attributes.get("friendly_name") if state else None
        texts = [str(text) for text in (entity_id.split(".", 1)[-1], name) if text]
        # The entity id goes first; the name counts only when the id names nothing, as a
        # renamed sensor's name is often just a place ("North Face", "Car port").
        label = next(
            (label for text in texts if (label := label_from_words(text, hears=hears))), None
        )
        # No camera is named for a person, so a person word anywhere says what a sensor
        # looks for when its last words don't, as in `person_front_door`. (`face` can be a
        # place: `north_face_gate`.)
        if label is None and any(set(_words(text)) & PERSON_ANYWHERE for text in texts):
            return "person"
        if label:
            return label
        if device_class in ("occupancy", "presence"):
            return "person"
        return "motion"


# Device classes of sensors that see or smell, so a sound word in their name names nothing.
DEAF_CLASSES: Final = {"motion", "moving", "occupancy", "presence", "smoke", "gas", "safety"}
# Words that end a trigger's entity id or name and say what its detector looks for, as
# integrations name their sensors: Frigate `<camera>_<object>_occupancy` (all of them have
# the occupancy device class, and `all` is any object), Reolink `<camera>_vehicle` and
# `<camera>_pet`, UniFi Protect `<camera>_package_detected` and `<camera>_car_alarm_detected`.
# English, and Norwegian for entity ids made in that language (HA writes `ø` as `o` and `æ`
# as `ae` in them).
TRIGGER_WORDS: Final = {
    "person": {
        "person",
        "persons",
        "people",
        "human",
        "humans",
        "face",
        "personer",
        "menneske",
        "mennesker",
        "ansikt",
    },
    "vehicle": {
        "vehicle",
        "vehicles",
        "car",
        "cars",
        "truck",
        "trucks",
        "lorry",
        "bus",
        "buses",
        "airplane",
        "van",
        "motorcycle",
        "motorbike",
        "bicycle",
        "bike",
        "bikes",
        "boat",
        "train",
        "plate",
        "kjoretoy",
        "kjøretøy",
        "bil",
        "biler",
        "buss",
        "lastebil",
        "varebil",
        "sykkel",
        "motorsykkel",
        "tog",
    },
    "animal": {
        "animal",
        "animals",
        "pet",
        "pets",
        "dog",
        "dogs",
        "cat",
        "cats",
        "bird",
        "birds",
        "horse",
        "goat",
        "skunk",
        "deer",
        "bear",
        "fox",
        "cow",
        "sheep",
        "rabbit",
        "squirrel",
        "raccoon",
        "kangaroo",
        "dyr",
        "fugl",
        "hest",
        "kjaeledyr",
        "kjæledyr",
        "hund",
        "katt",
    },
    # Frigate+ also detects the logos of delivery companies.
    "package": {
        "package",
        "packages",
        "parcel",
        "amazon",
        "ups",
        "fedex",
        "dhl",
        "usps",
        "dpd",
        "gls",
        "postnord",
        "postnl",
        "nzpost",
        "purolator",
        "pakke",
    },
    # UniFi Protect's audio sensors have no device class: `<camera>_baby_cry_detected`,
    # `<camera>_speaking_detected`, `<camera>_barking_detected`.
    "sound": {
        "sound",
        "audio",
        "horn",
        "bark",
        "barking",
        "cry",
        "crying",
        "speaking",
        "speech",
        "scream",
        "siren",
        "lyd",
        "bilalarm",
        "bjeffing",
        "noise",
    },
    "motion": {"motion", "movement", "all", "bevegelse"},
}
_LABEL_BY_WORD: Final = {word: label for label, words in TRIGGER_WORDS.items() for word in words}
# A person word anywhere says what a sensor looks for; see `_trigger_label`.
PERSON_ANYWHERE: Final = TRIGGER_WORDS["person"] - {"face", "ansikt"}
# Two words that name a detector together, read before either alone. None: names nothing
# this integration knows (Frigate's `teddy_bear` isn't an animal).
TRIGGER_PHRASES: Final[dict[tuple[str, str], str | None]] = {
    # UniFi Protect: `<camera>_audio_object_detected` hears anything; `<camera>_object_detected`
    # sees anything, and `object` names nothing.
    ("audio", "object"): "sound",
    ("car", "alarm"): "sound",
    ("smoke", "alarm"): "sound",
    ("co", "alarm"): "sound",
    ("fire", "alarm"): "sound",
    ("glass", "break"): "sound",
    # Frigate+ delivery logos.
    ("canada", "post"): "package",
    ("royal", "mail"): "package",
    ("an", "post"): "package",
    # Frigate objects.
    ("teddy", "bear"): None,
    ("hot", "dog"): None,
}
# Words after the one that names the detector.
TRIGGER_SUFFIXES: Final = {
    "occupancy",
    "presence",
    "detected",
    "detection",
    "detector",
    "detect",
    "sensor",
    "triggered",
    "active",
    "state",
    "alert",
    # On its own, `alarm` says nothing: Dahua's `<camera>_motion_alarm`.
    "alarm",
    # Reolink's dual-lens cameras: `<camera>_person_lens_0`.
    "lens",
    # Tapo's `<camera>_package_delivery`; Eufy's `package_taken`, `package_stranded` and
    # `dog_lick_detected`.
    "delivery",
    "delivered",
    "pickup",
    "taken",
    "lick",
    "poop",
    "stranded",
    "tilstedevaerelse",
    "tilstedeværelse",
    "belegg",
    "oppdaget",
}


def _words(text: str) -> list[str]:
    return [word for word in re.split(r"[^\w]+|_", text.lower()) if word]


def label_from_words(text: str, *, hears: bool = True) -> str | None:
    """The label of the last word in `text`, past suffixes such as `occupancy`, or None.

    Only that word decides, because a camera's own name comes first: `car_port_occupancy`
    and `bus_stop_motion` aren't vehicle sensors, but `car_port_person_occupancy` is a
    person sensor and `drive_car_alarm_detected` hears. A person's motion
    (`front_person_motion`) is a person, but not their `all`. Unless the sensor `hears`, a
    sound names nothing.
    """
    words = _words(text)
    i = len(words) - 1
    while i >= 0:
        word = words[i]
        if i and (phrase := (words[i - 1], word)) in TRIGGER_PHRASES:
            label = TRIGGER_PHRASES[phrase]
        elif word.isdigit() or word in TRIGGER_SUFFIXES:
            i -= 1
            continue
        else:
            label = _LABEL_BY_WORD.get(word)
            if word != "all" and label == "motion" and i and words[i - 1] in PERSON_ANYWHERE:
                return "person"
        if label == "sound" and not hears:
            return None
        return label
    return None
