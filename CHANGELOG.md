# Changelog

## Unreleased

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
