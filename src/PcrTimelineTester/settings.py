"""Persisted user preferences (calculator folder, port, last search parameters).

The GUI asks the same questions every launch, so everything the user can type or
tick is remembered in ``%LOCALAPPDATA%\\PcrTimelineTester\\settings.json``
(``~/.config/PcrTimelineTester`` on other systems) and restored on the next start.
Writing must never break a run, so failures are swallowed deliberately.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "PcrTimelineTester"
FILE_NAME = "settings.json"


def app_dir() -> Path:
    """Per-user data folder (settings, 图标缓存, 摸轴器日志), outside the install dir."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base or not Path(base).is_dir():
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def settings_path() -> Path:
    return app_dir() / FILE_NAME


def read_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(data: dict[str, Any]) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass
