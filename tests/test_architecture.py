"""Static guards: core modules stay Home Assistant free and the read path never writes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "wavin_wtc_net"
CORE_MODULES = [
    "__init__.py",
    "errors.py",
    "models.py",
    "protocol.py",
    "transport.py",
    "client.py",
    "poller.py",
    "pymodbus_backend.py",
]
READ_PATH_MODULES = ["protocol.py", "transport.py", "client.py", "poller.py", "pymodbus_backend.py"]


def _tree(name: str) -> ast.Module:
    return ast.parse((PACKAGE / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_has_no_home_assistant_import(module: str) -> None:
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(n == "homeassistant" or n.startswith("homeassistant.") for n in names)


@pytest.mark.parametrize("module", READ_PATH_MODULES)
def test_read_path_never_references_write_operations(module: str) -> None:
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Attribute):
            assert "write" not in node.attr.lower(), f"{module}: {node.attr}"
        if isinstance(node, ast.Name):
            assert "write" not in node.id.lower(), f"{module}: {node.id}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.lower().startswith("write"), f"{module}: {node.value}"
