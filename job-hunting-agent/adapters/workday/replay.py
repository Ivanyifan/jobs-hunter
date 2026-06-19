from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from adapters.workday.browser import launch_replay_browser
from adapters.workday.handlers import (
    CountrySelectorHandler,
    EducationRepeatableSectionHandler,
    ExecutionState,
    ExperienceRepeatableSectionHandler,
)
from adapters.workday.network import install_network_guard


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def load_stage_data() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "stage_data.json").read_text(encoding="utf-8"))


def load_fixture_html(stage: str) -> str:
    path = FIXTURE_DIR / f"{stage}.html"
    if not path.exists():
        raise ValueError(f"Unknown Workday fixture stage: {stage}")
    return path.read_text(encoding="utf-8")


def run_stage(stage: str, headless: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    data = load_stage_data()
    state = ExecutionState()
    with sync_playwright() as playwright:
        browser = launch_replay_browser(playwright, headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        network_metrics: dict[str, Any] = {}
        install_network_guard(context, network_metrics)
        page = context.new_page()
        page.set_content(load_fixture_html(stage), wait_until="domcontentloaded")
        if stage == "my_information":
            result = CountrySelectorHandler().ensure_united_states(page, state)
        elif stage == "education":
            result = EducationRepeatableSectionHandler().fill(page, data["education"], state)
        elif stage == "experience":
            result = ExperienceRepeatableSectionHandler().fill(page, data["experience"], state)
        else:
            raise ValueError(f"Unknown Workday stage: {stage}")
        metrics = page.evaluate("window.fixtureMetrics || {}")
        snapshot = page.evaluate(
            """() => ({
                text: document.body.innerText.replace(/\\s+/g, " ").trim(),
                fixtureState: window.fixtureState || null
            })"""
        )
        context.close()
        browser.close()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    replay_result = {
        "stage": stage,
        "ok": result.ok,
        "changed": result.changed,
        "field": result.field,
        "reason": result.reason,
        "status": result.status,
        "attempts": result.attempts,
        "elapsed_ms": elapsed_ms,
        "field_locks": state.field_locks,
        "field_attempts": state.field_attempts,
        "section_add_attempts": state.section_add_attempts,
        "metrics": metrics,
        "network": network_metrics,
        "snapshot": snapshot,
    }
    if network_metrics.get("external_requests"):
        replay_result["ok"] = False
        replay_result["status"] = "failed"
        replay_result["reason"] = "external_network_blocked"
    return replay_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a sanitized local Workday stage fixture.")
    parser.add_argument("--stage", choices=["my_information", "education", "experience"], required=True)
    parser.add_argument("--headed", action="store_true", help="Run headed for local debugging.")
    args = parser.parse_args()
    result = run_stage(args.stage, headless=not args.headed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
