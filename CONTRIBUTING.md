# Contributing

Contributions to Event Reel are welcome: bug reports, fixes, features, translations and
documentation. Before opening a PR, establish why the change belongs in the integration
and complete the review and verification below.

These rules apply to every PR, including drafts. Meeting them makes a PR eligible for
merge. The maintainer decides whether to merge it.

<a id="ai"></a>

## AI-assisted contributions are welcome

You may use AI coding agents, such as Claude Code, Codex, Copilot or Cursor, for any part
of a contribution: code, tests, documentation and reviews. The rules are the same for
changes written by an agent and by hand:

- **You own the change.** Read the whole diff before you open the PR, be able to explain
  it, and run the checks yourself. "The agent wrote it" doesn't answer a review comment.
- **Give the agent [AGENTS.md](AGENTS.md).** It holds the code, test and privacy
  conventions. Most agents read it on their own; Claude Code reads it through
  [CLAUDE.md](CLAUDE.md).
- **Say what the agent did.** In the PR's Review section, name the tool, which parts it
  wrote and how you checked them. This helps the review and doesn't count against the
  PR.
- **Watch for private data.** An agent connected to your Home Assistant (through an MCP
  server, say) easily copies your real entity ids, names, AI answers or logs into
  examples, tests and PR text. Check before you commit (see
  [Keep your home out of it](#privacy)).
- **Send only what you stand behind.** PRs that were never run, whose description
  doesn't match the diff, or that arrive as an unreviewed batch get closed.

## Reporting a bug

Open an issue with the bug report form. It asks what happened and what you expected, how
the events reach Event Reel, the Event Reel and Home Assistant versions, the AI Task
integration and model, and debug logs with your own names replaced.

## Setup and workflow

You need Python 3.13 or newer, and Linux for the tests: the Home Assistant test harness
imports `fcntl`, so on Windows run it in WSL. Install the harness for the Home Assistant
version you test against, the test tools and the requirements of the integrations Event
Reel depends on:

```bash
pip install pytest-homeassistant-custom-component -r requirements_test.txt
python scripts/component_requirements.py | xargs pip install
```

Read [AGENTS.md](AGENTS.md) for the layout, the Home Assistant API rules and the test
conventions. The design Event Reel shares with the card is in the
[card's PLAN.md](https://github.com/oddhus/event-reel-card/blob/main/PLAN.md).

For a new feature, service or event field, open an issue first so we can agree on the
approach before you build it. Bug fixes and small improvements can go straight to a PR.

<a id="belongs-in-the-integration"></a>

## Ask first: does it belong in the integration?

Before adding a feature, option or service, ask: "Is this a good addition to Event Reel,
or does it belong in the card, my Home Assistant configuration or an automation?"

An addition to the integration should meet these criteria:

- It serves the integration's purpose: describing camera events with AI Task and keeping
  them for the card and for automations. How events look on the timeline belongs in the
  card.
- It helps other installs, not only yours. Install-specific needs belong in the options
  or in your own automations, which can call the services, not in special cases in the
  code.
- It uses only documented Home Assistant APIs that the oldest supported version
  (`hacs.json`) has.
- It changes the event format, the services and the websocket commands only
  additively. The card and users' automations depend on them.

The rule has exceptions. Explain the general need in the PR when you make one.

<a id="one-problem"></a>

## Solve one problem per PR

Split a PR that solves several independent problems, even when each fix is useful on
its own. Count the underlying problems, not the linked issues.

Include the source, tests, translations and documentation the problem requires: add
user-facing changes under `Unreleased` in [CHANGELOG.md](CHANGELOG.md), and update the
[README](README.md) when usage, a service or the event format changes. Put unrelated
cleanup and other fixes in separate PRs.

<a id="pr-title"></a>

## Title the PR as a plain sentence

Write the PR title as a plain-language sentence in the imperative and in sentence case,
such as "Send frames from the middle of a clip". Keep code names in their own casing and
leave out conventional commit prefixes such as `fix:`. PRs are squash-merged, so the
title becomes the commit subject on `main` and a line in the release notes.

<a id="review-before-submitting"></a>

## Review it before you open it

Complete a full review pass before opening a PR, or before marking a draft ready for
review.

Use a reviewer other than the code's author: another person, or an AI agent with a
fresh context. When an agent wrote the change, the person who directed it can review it.
The reviewer checks the complete diff and the relevant surrounding code against the
agreed scope, this guide and [AGENTS.md](AGENTS.md). Say in the PR who reviewed it.

<a id="verification"></a>

## Show that it works

Explain how you established the problem and how you checked the change. Record the
commands and manual checks you ran, the results and anything you could not check.

- `ruff check .`, `ruff format --check .` and `pytest -q` pass. CI runs the same against
  the oldest supported and the newest tested Home Assistant.
- Behavior changes include tests that follow [AGENTS.md](AGENTS.md#tests).
- Changes to how events are described, to frames or to the services are also checked in
  a real Home Assistant, since the tests fake the AI, ffmpeg and the cameras.
- Name the versions you tested against when they matter: Home Assistant, the AI Task
  integration and model, and the camera integration.

<a id="privacy"></a>

## Keep your home out of it

This repository is public. Keep real entity ids, names, hostnames, IPs, tokens, camera
footage, AI answers about your home and logs from it out of code, tests, fixtures,
documentation, commit messages and PR text. Use neutral examples such as
`camera.front_door`.

Keep notes about your own install in a gitignored `*.local.md`, and real data snapshots
in `*.local.json`. Check `git diff --cached` before every commit.

When a bug shows only with your data, describe its shape, such as "a doorbell ring whose
clip is under a second long", or anonymize the data before you attach it.

## Merging and closing

Before merging, the maintainer checks the problem, whether it belongs in the
integration, the scope and the verification evidence. The maintainer also cuts the
releases.

The maintainer may close a PR or request changes when it belongs elsewhere, solves
several independent problems, lacks a completed review or evidence, or contains private
data. The closing comment names the rule and explains what would change the outcome.

Address the reason and reopen the PR, or open focused replacement PRs and link them to
the original.
