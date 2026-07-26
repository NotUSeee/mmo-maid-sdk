"""Backward-compatibility shim for the renamed ``yourbot_sdk`` package.

``mmo_maid_sdk`` was renamed to ``yourbot_sdk`` in 0.6.0. This module keeps
``import mmo_maid_sdk`` (and every submodule / legacy nested import) working
by re-exporting the real package. It ships inside the ``yourbot-sdk`` wheel,
so installing either ``yourbot-sdk`` or the ``mmo-maid-sdk`` alias makes the
old import path resolve. Prefer ``import yourbot_sdk`` in new code.

The identical file is kept at the monorepo root (``mmo_maid_sdk/__init__.py``)
as a dev-time shim so in-process imports from the source tree behave the same
as the installed wheel.
"""
import importlib as _importlib
import sys as _sys
import warnings as _warnings

import yourbot_sdk as _real
from yourbot_sdk import *  # noqa: F403
from yourbot_sdk import __all__, __version__  # noqa: F401

_warnings.warn(
    "'mmo_maid_sdk' is deprecated and was renamed to 'yourbot_sdk'; import "
    "'yourbot_sdk' instead. The compatibility shim will be removed in a "
    "future major release.",
    DeprecationWarning,
    stacklevel=2,
)

# A star import does NOT register submodules in sys.modules, so
# ``from mmo_maid_sdk.testing import ...`` and the legacy deep
# ``from mmo_maid_sdk.mmo_maid_sdk._transport import ...`` would fail without
# explicit aliasing. Map every submodule under both the flat ``mmo_maid_sdk.*``
# and the legacy nested ``mmo_maid_sdk.mmo_maid_sdk.*`` names to the real
# module object. The try/except covers both layouts: the installed wheel
# (flat ``yourbot_sdk.<name>``) and the monorepo dev tree
# (nested ``yourbot_sdk.yourbot_sdk.<name>``).
_SUBMODULES = (
    "_plugin", "_context", "_transport", "_components", "_exceptions",
    "_validation", "events", "responses", "dashboard", "testing", "cli",
)
for _name in _SUBMODULES:
    try:
        _mod = _importlib.import_module(f"yourbot_sdk.{_name}")
    except ModuleNotFoundError:
        _mod = _importlib.import_module(f"yourbot_sdk.yourbot_sdk.{_name}")
    _sys.modules[f"mmo_maid_sdk.{_name}"] = _mod
    _sys.modules[f"mmo_maid_sdk.mmo_maid_sdk.{_name}"] = _mod

# ``from mmo_maid_sdk.mmo_maid_sdk import Plugin`` resolves to the real package.
_sys.modules["mmo_maid_sdk.mmo_maid_sdk"] = _real

del _importlib, _sys, _warnings, _name, _mod, _real
