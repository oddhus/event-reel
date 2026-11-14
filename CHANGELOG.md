# Changelog

## Unreleased

- A trigger for a vehicle, an animal or a package is labelled `vehicle`, `animal` or
  `package`, no longer `motion`, and the AI is told what it looks for. It is read from the
  words of the entity id or name (README, Setup). Frigate's object sensors, which all have
  the occupancy device class, were all taken for person sensors before; its sensors for
  common vehicles, animals and delivery logos no longer are, and its `all` sensor is now
  `motion`, as is an occupancy sensor whose last word is `motion` (unless the word before
  it is a person word). A sound sensor is labelled `sound`, and so is any other sensor that
  doesn't see or smell (by its device class) whose last words are a sound, such as UniFi
  Protect's `<camera>_baby_cry_detected`. When the AI names no subject, an event's category
  is its `vehicle`, `animal` or `package` label, also for `event_reel.describe`; it was
  `motion`, or `person` for an occupancy sensor.
- A doorbell that isn't Nest rings too: when a camera's trigger is a doorbell's event
  entity (device class `doorbell`, as from Ring or UniFi Protect), its events are rings
  like a Nest chime. They are kept when the AI sees nobody, their category is `doorbell`,
  and a ring during the cooldown marks the camera's event.
- A ring that marks an event where the AI saw nobody (kept with the option to keep such
  events) also clears that event's "no activity" title, as for any quiet ring.
- A doorbell ring that comes just as a look ends is no longer lost. Before, a ring while
  the look was cleaning up joined it after its outcome was decided, and no event was made.
  A ring that gets its own event starts a new cooldown.
- A false alarm no longer hides what comes next for the whole cooldown. When the AI finds
  nothing and no event is kept, the camera's cooldown ends at most 10 s after that look,
  so the next Nest event or trigger is described. A cooldown set shorter than that stays
  as it is. A trigger that stays on doesn't fire again while it stays on.
- When the AI fails with an error, such as a local model that is restarting, it is asked
  once more with the same frames 10 s later. Only if that fails too is the event kept
  without text. A request the AI Task rejects, an error that took over a minute, an
  answer that takes over 3 minutes and an unexpected error aren't asked again, nor is
  anything while the repair below is up.
- After three AI failures in a row, a repair under Settings → Repairs says that the AI Task
  doesn't describe camera events, with the last error. The next answer, or a reload, removes it. The AI
  Task list in the options shows only AI Tasks that accept pictures.
- An unexpected error from an AI Task no longer loses the event: like any other AI failure,
  the event gets no description and `analysis: failed`.
- Diagnostics: download them from the integration's menu to attach to a bug report. They
  leave out the extra instructions and what the cameras saw (README, Reporting a problem).
- A doorbell ring whose clip shows nobody no longer gets the AI's "no activity" title. Its
  title is empty, so the card shows "The doorbell rang", and the description still says
  what the clip showed.
- Frames from a clip come from the middle of equal parts of it, no longer from its first
  and last moments, which are often empty. A clip of a second or so is sampled more
  densely, so it still gives as many frames as the camera is set to.
- At a doorbell ring, the AI is no longer told that the detector can be wrong. It is told
  that a visitor is most likely there, perhaps so close to the camera that they are only
  partly in view.
- Debug logging shows how long each clip was, which moments of it the AI saw, and the
  AI's answer. The README has a new section on what to do when the AI misses something.
- Reloading or disabling Event Reel no longer leaves the snapshots of waiting analyses in
  the media folder's `event_reel/_work`.

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
