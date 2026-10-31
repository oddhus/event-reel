# Working on Event Reel

A Home Assistant custom integration that describes camera events with AI Task and stores
them for the [Event Reel card](https://github.com/oddhus/event-reel-card). The card's
`PLAN.md` holds the shared design; this repo is the server side. It installs through HACS.

## Layout

`custom_components/event_reel/` holds the integration. `sources.py` decides when (Nest
events, trigger entities), `frames.py` gets frames (ffmpeg, snapshots), `analyzer.py` runs
one job at a time through `ai_task.generate_data`, and `store.py` keeps events and key
frames. `services.py` and `websocket.py` are the public API. Keep `prompt.py` pure.
The event format, the services and the websocket commands are a contract with the card
and with users' automations. Change them only additively, and update the README's
Services, Events and event format sections with them.

## Home Assistant APIs

Use only documented Home Assistant APIs: services (`ai_task.generate_data`), media source,
`async_sign_path`. Check a new one against the source of the oldest supported HA version
(`homeassistant` in `hacs.json`) and the newest.
Take validation schemas from `_schema.vol`, which is what HA's own helpers use (voluptuous
before 2026.10, probatio from 2026.10).
Keep `hacs.json` at the oldest Home Assistant whose APIs the integration uses, and the
oldest harness in the CI matrix in step with it.

## Strings

Put every user-facing string in `strings.json`, `translations/en.json` (a copy) and
`translations/nb.json`.

## Tests

Absolutely no tautological tests. Assert behaviour against expectations written
independently of the code under test. Never compute the expected result with the
implementation, or only assert that a mock returns what it was configured to return.
Fake only the edges: the AI service, ffmpeg, camera snapshots and HTTP.
The test harness needs Linux (it imports `fcntl`); on Windows, run it in WSL. Install
`pytest-homeassistant-custom-component` pinned to the Home Assistant version under test,
`requirements_test.txt`, and `python scripts/component_requirements.py | xargs pip install`.
Before review, run `ruff check .`, `ruff format --check .` and `pytest -q`. CI runs the
same against the oldest supported and the newest tested Home Assistant, plus hassfest and
the HACS check. A weekly job also tests against the newest Home Assistant release.
The repo has no JavaScript or TypeScript, so ruff is its linter; its rules are in
`pyproject.toml`.

## Privacy

This repository is public. Never commit real entity ids, names, hostnames, IPs, tokens,
camera footage or fixtures from a real home. Use neutral examples such as
`camera.front_door`.
Notes about a real install belong in a gitignored `*.local.md`, and real data snapshots in
`*.local.json`. Read them for testing; never copy their contents into tracked files.
When you can reach a real Home Assistant (through an MCP server, say), keep its ids, names
and responses out of code, tests, docs, commit messages and PR text.
Check `git diff --cached` before every commit.

## Documentation

Update the README when usage changes, and add user-facing changes under `Unreleased` in
[CHANGELOG.md](CHANGELOG.md). Put design decisions that the card shares in its `PLAN.md`.
Change these instructions when a durable working convention changes.

## Task references

Before opening a PR, read [CONTRIBUTING.md](CONTRIBUTING.md) in full. Write the PR body
from [the PR template](.github/pull_request_template.md), filling each section, in place
of any generic summary format.
Releases are for the maintainer: set the version in
`custom_components/event_reel/manifest.json` and `pyproject.toml`, move the `Unreleased`
notes in CHANGELOG.md under that version, commit, then tag `vX.Y.Z` and push the tag. The
release workflow checks that the tag matches the manifest and creates the GitHub release,
where HACS finds it.
