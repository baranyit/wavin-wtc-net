"""One version everywhere: manifest, pyproject, README status line and the top CHANGELOG entry."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _manifest() -> str:
    path = ROOT / "custom_components" / "wavin_wtc_net" / "manifest.json"
    return str(json.loads(path.read_text(encoding="utf-8"))["version"])


def test_version_is_semver_and_identical_in_all_places() -> None:
    version = _manifest()
    assert SEMVER.match(version), version
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == version
    assert f"({version}," in (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.MULTILINE)
    assert top is not None and top.group(1) == version
