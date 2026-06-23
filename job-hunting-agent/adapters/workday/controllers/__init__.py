from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from .base import BaseStageController


def _load_legacy_controllers() -> ModuleType:
    legacy_path = Path(__file__).resolve().parent.parent / "controllers.py"
    spec = importlib.util.spec_from_file_location("adapters.workday._legacy_controllers", legacy_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Workday controller compatibility module: {legacy_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_legacy = _load_legacy_controllers()

MyInformationController = _legacy.MyInformationController
MyExperienceController = _legacy.MyExperienceController
NavigationController = _legacy.NavigationController
replay_fixture = _legacy.replay_fixture

__all__ = [
    "BaseStageController",
    "MyInformationController",
    "MyExperienceController",
    "NavigationController",
    "replay_fixture",
]
