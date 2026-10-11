# Working on Event Reel

A Home Assistant custom integration that describes camera events with AI Task and stores
them for the [Event Reel card](https://github.com/oddhus/event-reel-card). The card's
`PLAN.md` holds the shared design; this repo is the server side.

## Layout

- `custom_components/event_reel/`: `sources.py` decides when (Nest events, trigger
  entities), `frames.py` gets frames (ffmpeg, snapshots), `analyzer.py` runs one job at a
  time through `ai_task.generate_data`, `store.py` keeps events and key frames,
  `services.py` and `websocket.py` are the public API, `prompt.py` is pure.
- The event format, the services and the websocket commands are a contract with the card
  and with users' automations. Change them only additively, and update the README.

## Rules

- Use only documented Home Assistant APIs: services (`ai_task.generate_data`), media
  source, `async_sign_path`. Check a new one against the source of the oldest supported
  HA version (`hacs.json`) and the newest.
- Validation schemas come from `_schema.vol`, which is what HA's own helpers use
  (voluptuous before 2026.10, probatio from 2026.10).
- Every user-facing string goes in `strings.json`, `translations/en.json` (a copy) and
  `translations/nb.json`.
- No tautological tests: assert behaviour against expectations written independently of
  the code. Fake only the edges (the AI service, ffmpeg, camera snapshots, HTTP).

## Commands

The test harness needs Linux (it imports `fcntl`); on Windows, run it in WSL.

- `ruff check . && ruff format --check .`
- `pytest -q` with `pytest-homeassistant-custom-component` pinned to the HA version under
  test, plus `python scripts/component_requirements.py | xargs pip install`.

## Privacy

This repository is public. Never commit real entity ids, names, hostnames, IPs, tokens,
camera footage or fixtures from a real home. Use neutral examples such as
`camera.front_door`. Notes about a real install belong in a gitignored `*.local.md`.
