"""Standalone UB frame tuner for PCR clan battle timelines.

Battles are simulated by PCRCalculator over HTTP, so searching is pure local
computation plus remote battles: no game client, no injected DLL.
"""

from importlib.metadata import version
from pathlib import Path
import sys


def _project_version() -> str:
    package = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        project = package / "pyproject.toml"
    elif package.parent.name == "src" and (package.parent.parent / "pyproject.toml").is_file():
        project = package.parent.parent / "pyproject.toml"
    else:
        return version("PcrTimelineTester")

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    with project.open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


__version__ = _project_version()

__all__ = ["__version__"]



