# Changelog

## Unreleased

- Frames from a clip come from the middle of equal parts of it, no longer from its first
  and last moments, which are often empty. A clip of a second or so is sampled more
  densely, so it still gives as many frames as the camera is set to.
- At a doorbell ring, the AI is no longer told that the detector can be wrong. It is told
  that a visitor is most likely there, perhaps so close to the camera that they are only
  partly in view.
- Debug logging shows how long each clip was, which moments of it the AI saw, and the
  AI's answer. The README has a new section on what to do when the AI misses something.

## 0.1.1 - 2026-10-17

- An icon. Home Assistant 2026.9 and newer show it for the integration, from
  `custom_components/event_reel/brand/`; its source is `docs/icon.svg`.

## 0.1.0 - 2026-10-13

First version.

- Describes Nest camera and doorbell events from their clips, and other cameras from
  snapshots when a trigger turns on, with Home Assistant's AI Task.
- A doorbell ring is always kept; other events where the AI sees nothing are dropped
  unless the option to keep them is on.
- Services `describe`, `add_event`, `remove_event` and `get_events`, the bus event
  `event_reel_event`, and websocket commands for the Event Reel card.
- People see only events of cameras they're allowed to see. Removing events is for
  administrators and automations.
- Removing the integration deletes its events and key frames.
- English and Norwegian Bokmål.
