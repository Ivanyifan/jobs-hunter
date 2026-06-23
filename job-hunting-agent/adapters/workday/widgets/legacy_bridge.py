from __future__ import annotations

"""Compatibility helpers for callers that want old function-style widget access.

This module intentionally does not wire the new widgets into production stage
execution. It only exposes thin wrappers that return the shared ActionResult
contract for transition tests or future controller migration work.
"""

from pathlib import Path
from typing import Any, Iterable

from .date import WorkdayDateGroupWidget
from .file_upload import WorkdayFileUploadWidget
from .prompt import WorkdayPromptWidget
from .radio import WorkdayRadioGroupWidget
from .text import WorkdayTextInputWidget


def choose_prompt_option(
    page: Any,
    selector: str,
    expected_value: Any,
    aliases: Iterable[Any] | None = None,
    **context: Any,
):
    context = {"selector": selector, **context}
    return WorkdayPromptWidget().select_exact_or_alias(page, expected_value, aliases, context)


def choose_radio_value(page: Any, selector: str, expected_value: Any, **context: Any):
    context = {"selector": selector, **context}
    return WorkdayRadioGroupWidget().select_value(page, expected_value, context=context)


def fill_text_value(page: Any, selector: str, value: Any, mode: str = WorkdayTextInputWidget.MODE_EXACT, **context: Any):
    context = {"selector": selector, **context}
    return WorkdayTextInputWidget(mode=mode).act(page, value, context)


def fill_date_value(page: Any, selector: str, value: Any, **context: Any):
    context = {"selector": selector, **context}
    return WorkdayDateGroupWidget().fill_parts(page, value, context)


def upload_file_value(page: Any, selector: str, path: str | Path, **context: Any):
    context = {"selector": selector, **context}
    return WorkdayFileUploadWidget().upload(page, path, context)
