from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable


TRAILING_NOISE_RE = re.compile(r"[\s:;,.!?*]+$")
REQUIRED_NOISE_RE = re.compile(r"\b(required|mandatory)\b", re.IGNORECASE)
PUNCT_RE = re.compile(r"[^\w\s/?'-]+", re.UNICODE)


def normalize_question_text(text: str | None) -> str:
    value = str(text or "").lower().strip()
    value = value.replace("\u00a0", " ")
    value = REQUIRED_NOISE_RE.sub(" ", value)
    value = value.replace("*", " ")
    value = PUNCT_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return TRAILING_NOISE_RE.sub("", value)


def normalize_option(option: str | None) -> str:
    value = str(option or "").lower().strip()
    value = value.replace("\u00a0", " ")
    value = PUNCT_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_options(options: Iterable[str] | None) -> list[str]:
    normalized = {normalize_option(option) for option in (options or []) if normalize_option(option)}
    return sorted(normalized)


def fingerprint_question(text: str | None, control_type: str | None, options: Iterable[str] | None = None) -> str:
    payload = {
        "text": normalize_question_text(text),
        "control_type": normalize_option(control_type),
        "options": normalize_options(options),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def options_compatible(left: Iterable[str] | None, right: Iterable[str] | None) -> bool:
    left_norm = normalize_options(left)
    right_norm = normalize_options(right)
    if not left_norm and not right_norm:
        return True
    return left_norm == right_norm
