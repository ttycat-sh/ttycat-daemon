"""What kind of build this daemon is part of, from build.json beside it. app/build.sh writes one into
every release, from TTYCAT_VERSION:

  {"release": true, "env": "dev" or "prod", "version": "0.2.0-dev.1", "updates": "https://..."}

Without one, as in a checkout or a build on a developer's Mac, it's a developer's build: TTYCAT_TRUST_KEY
and TTYCAT_UPDATES_URL work there, and never in a release.
"""
from __future__ import annotations

import json
from pathlib import Path

FILE = Path(__file__).with_name("build.json")


def read(path: Path = FILE) -> dict:
    """The build's description, or {} for a developer's build. One that can't be read counts as a
    release with nothing set, so a damaged file never turns the developer overrides on."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        return {"release": True}
    try:
        info = json.loads(text)
    except ValueError:
        return {"release": True}
    return info if isinstance(info, dict) and info.get("release") is True else {"release": True}


def is_release(info: dict | None = None) -> bool:
    return (read() if info is None else info).get("release") is True
