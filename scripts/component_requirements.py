"""Print the Python requirements of the integrations Event Reel depends on.

The test harness installs Home Assistant core without them. Run it with the Python of the
test environment: `python scripts/component_requirements.py | xargs pip install`.
"""

import json
from pathlib import Path

import homeassistant.components as components

MANIFEST = Path(__file__).parents[1] / "custom_components" / "event_reel" / "manifest.json"
# ai_task needs conversation, which needs its own requirements.
EXTRA = ["conversation"]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    base = Path(components.__file__).parent
    seen: set[str] = set()
    requirements: set[str] = set()
    todo = [*manifest.get("dependencies", []), *EXTRA]
    while todo:
        domain = todo.pop()
        if domain in seen:
            continue
        seen.add(domain)
        path = base / domain / "manifest.json"
        if not path.exists():
            continue
        dep = json.loads(path.read_text(encoding="utf-8"))
        requirements.update(dep.get("requirements", []))
        todo.extend(dep.get("dependencies", []))
    print("\n".join(sorted(requirements)))


if __name__ == "__main__":
    main()
