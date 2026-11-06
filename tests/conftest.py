"""Test configuration.

``async def`` tests run on a fresh event loop via ``asyncio.run`` so the suite has
no dependency on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        names = pyfuncitem._fixtureinfo.argnames
        kwargs: dict[str, Any] = {name: pyfuncitem.funcargs[name] for name in names}
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
    return None
