# Event Reel

A Home Assistant integration that describes camera events with AI and keeps them for the
[Event Reel card](https://github.com/oddhus/event-reel-card).

When a camera sees something, Event Reel takes a few frames, asks your AI what is
happening, and stores a short title and description with a key frame. The card shows
them on its timeline together with the clip. Automations can read them too, for daily
summaries or notifications.

- **Any AI.** Event Reel uses Home Assistant's [AI Task](https://www.home-assistant.io/integrations/ai_task/),
  so it works with the AI you already set up: Ollama, OpenAI, Google Gemini, Anthropic
  and others. For camera frames, pick a model that can see images.
- **Nest doorbells and cameras.** Described from their own event clips, which their
  snapshots can't do.
- **Any other camera.** Described from snapshots when a trigger turns on, such as a
  person sensor from Frigate, Reolink or UniFi.
- **Your own sources.** Services let any automation describe a video or an image, or add
  an event it wrote itself.

The card works without this integration. Install it when you want AI descriptions.

## Requirements

- Home Assistant 2026.9 or newer.
- An AI Task entity. With Ollama: add the Ollama integration, add an **AI Task** to it,
  and choose a vision model without a thinking mode, such as `qwen3-vl:4b-instruct` or
  `gemma3:4b`. Thinking variants (plain `qwen3-vl:4b`) can return empty structured answers.

## Installation

1. In HACS, open the menu → **Custom repositories**, add
   `https://github.com/oddhus/event-reel` as an **Integration**, and download **Event Reel**.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Event Reel**.

## Setup

**Options** (Settings → Devices & services → Event Reel → Configure):

| Option | Default | |
|---|---|---|
| AI Task | the preferred one | Which AI Task entity to use. |
| Language | HA's language | The language of titles and descriptions: a code (`en`, `nb`, `de`) or a name. |
| Keep events for | 30 days | Older events and their key frames are deleted. |
| Extra instructions | | Added to every request, for example what is normal at your home. |
| Keep events where the AI sees nothing | off | Off: a false alarm leaves no event. A doorbell ring is always kept. |

**Cameras** (Event Reel → **Add camera**):

| Field | Default | |
|---|---|---|
| Camera | | The camera entity. Events are tagged with it, so the card can show its clip. |
| Name | the camera's name | The name the AI is told. |
| Triggers | | Binary sensors or event entities that start a description. Not needed for Nest cameras. |
| Nest events | person, doorbell ring | For Nest cameras: which events to describe. |
| Frames | 3 | How many frames the AI sees. |
| Cooldown | 90 s | New triggers from the camera are ignored this long after one starts. |
| Extra instructions | | Added for this camera only. |

How the frames are taken:

- **Nest cameras** use the clip of each Nest event, spread over the clip. A doorbell ring
  during the cooldown marks the event as a ring.
- **Other cameras** use snapshots one second apart, when a trigger turns on.

Nest cameras that stream with WebRTC (most newer models) have no snapshots in Home
Assistant, so triggers on them would only send a placeholder picture. Use their Nest
events instead.

## Services

### `event_reel.describe`

Describe a camera now, from its snapshots, a video or an image. Use it from any
automation to connect a source that isn't covered above.

```yaml
action: event_reel.describe
data:
  camera: camera.front_door
  video: "{{ trigger.event.data.attachment.video }}"  # optional: a Home Assistant path or media source id
  frames: 3
  labels: [person]
response_variable: described   # {"event": {...}} or {"event": null} when nothing was seen
```

### `event_reel.add_event`

Add an event you wrote yourself, for example from another AI pipeline.

```yaml
action: event_reel.add_event
data:
  title: Parcel left on the step
  description: A courier in a yellow jacket leaves a box and walks away.
  camera: camera.front_door
  category: package
  image: media-source://media_source/local/snapshots/front_door.jpg
```

### `event_reel.get_events`

Read events, newest first. A daily summary:

```yaml
actions:
  - action: event_reel.get_events
    data:
      start: "{{ now() - timedelta(hours=24) }}"
      categories: [person, doorbell]
      limit: 20
    response_variable: reel
  - action: notify.mobile_app_phone
    data:
      title: "{{ reel.events | count }} camera events today"
      message: >-
        {% for e in reel.events[:5] %}{{ as_timestamp(e.start) | timestamp_custom('%H:%M') }} {{ e.title }}
        {% endfor %}
```

### `event_reel.remove_event`

Remove an event and its key frame by id.

## Events

Every added, updated or removed event fires `event_reel_event` with
`{"action": "added" | "updated" | "removed", "event": {...}}`. Use it to send a
notification with the AI's description:

```yaml
triggers:
  - trigger: event
    event_type: event_reel_event
    event_data:
      action: added
actions:
  - action: notify.mobile_app_phone
    data:
      title: "{{ trigger.event.data.event.title }}"
      message: "{{ trigger.event.data.event.description }}"
```

## The event format

```json
{
  "id": "4f0c…",
  "start": "2026-10-08T13:44:55+02:00",
  "end": null,
  "camera": "camera.front_door",
  "title": "Child looks out of the door",
  "description": "A child in a grey sweater opens the door and looks outside.",
  "category": "doorbell",
  "labels": ["person", "chime"],
  "key_frame": "media-source://media_source/local/event_reel/2026-10-08/4f0c….jpg",
  "source": "nest",
  "nest": {"device_id": "…", "event_token": "…"},
  "analysis": "ok"
}
```

- `category`: `person`, `vehicle`, `animal`, `package`, `doorbell` or `motion`, or what
  `add_event` was given.
- `source`: `nest`, `trigger` or `service`. `trigger` is set to the trigger entity for
  trigger events.
- `analysis`: `ok`, `failed` (the AI didn't answer; the event is kept without text) or
  `none` (added with `add_event`).
- `key_frame`: a media source id. Key frames are stored in your media folder under
  `event_reel/`.

The card reads events through the websocket commands `event_reel/events/list`
(`start_time`, `end_time`, `cameras`, `limit`) and `event_reel/events/subscribe`.

## Coming from LLM Vision

- Add each camera that an LLM Vision automation analysed, with the same trigger entity.
  Add Nest doorbells without triggers.
- Replace `llmvision.get_events` with `event_reel.get_events`. The events have the same
  `start`, `title`, `description` and `key_frame` fields; `key_frame` is a media source id.
- Frigate can write its own review summaries with GenAI (`review.genai` in Frigate 0.17+).
  The card shows those without this integration.

## Privacy

Frames are sent only to the AI Task you choose. With a local model such as Ollama they
stay in your network. Key frames are kept in your media folder until the retention ends.

## License

MIT
