from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def chromium_launch_kwargs() -> dict[str, Any]:
    candidates = [
        os.getenv("WORKDAY_REPLAY_BROWSER"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return {"executable_path": candidate}
    return {}


def launch_replay_browser(playwright, headless: bool = True):
    return playwright.chromium.launch(headless=headless, **chromium_launch_kwargs())
