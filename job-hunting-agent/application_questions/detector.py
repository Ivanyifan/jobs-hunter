from __future__ import annotations

import re
from typing import Any

from .fingerprint import fingerprint_question, normalize_question_text
from .models import DetectedQuestion, NEEDS_TECHNICAL_REVIEW, TECHNICAL_REVIEW, UNANSWERED


SENSITIVE_PATTERNS = [
    r"\bsponsor(ship)?\b",
    r"\bvisa\b",
    r"\bwork authorization\b",
    r"\bauthorized to work\b",
    r"\bdisabilit(y|ies)\b",
    r"\bveteran\b",
    r"\brace\b",
    r"\bethnic",
    r"\bgender\b",
    r"\bcriminal\b",
    r"\bconvict",
    r"\bsalary\b",
    r"\bcompensation\b",
    r"\bconflict of interest\b",
    r"\belectronic signature\b",
]

NON_BLOCKING_VALIDATION_PATTERNS = [
    r"\b\d+\s+item[s]?\s+selected\b",
    r"\bpress delete to clear value\b",
    r"^alert:\s*verify that the field\b.*\bcorrectly capitalized\b",
]


def is_sensitive_question(text: str | None) -> bool:
    lowered = str(text or "").lower()
    return any(re.search(pattern, lowered) for pattern in SENSITIVE_PATTERNS)


def is_blocking_validation_message(text: str | None) -> bool:
    message = str(text or "").strip()
    if not message:
        return False
    lowered = message.lower()
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in NON_BLOCKING_VALIDATION_PATTERNS):
        return False
    return True


def approved_answer_for(question: DetectedQuestion, approved_answers: dict[str, Any] | None) -> Any:
    answers = approved_answers or {}
    if question.fingerprint in answers:
        return answers[question.fingerprint]
    if question.normalized_text in answers:
        return answers[question.normalized_text]
    if question.raw_text in answers:
        return answers[question.raw_text]
    return None


def detect_visible_required_questions(page, approved_answers: dict[str, Any] | None = None, user_data: dict[str, Any] | None = None) -> list[DetectedQuestion]:
    rows = page.evaluate(
        """() => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const visible = el => {
            if (!el || !el.isConnected) return false;
            if (el.type === 'hidden') return false;
            if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
            const style = window.getComputedStyle(el);
            const box = el.getBoundingClientRect();
            return !!(box.width && box.height) && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const byIdText = id => {
            if (!id) return '';
            const node = document.getElementById(id);
            return node ? clean(node.innerText || node.textContent || node.getAttribute('aria-label') || '') : '';
          };
          const labelFor = el => {
            const parts = [];
            const id = el.getAttribute('id');
            if (id && window.CSS && CSS.escape) {
              document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(label => parts.push(clean(label.innerText || label.textContent)));
            }
            const aria = el.getAttribute('aria-label');
            if (aria) parts.push(clean(aria));
            const labelledBy = clean(el.getAttribute('aria-labelledby'));
            if (labelledBy) {
              labelledBy.split(/\\s+/).forEach(idPart => parts.push(byIdText(idPart)));
            }
            const fieldset = el.closest('fieldset');
            if (fieldset) {
              const legend = fieldset.querySelector('legend');
              if (legend) parts.push(clean(legend.innerText || legend.textContent));
            }
            const group = el.closest('[role="group"], [role="radiogroup"], .form-group, .field, .question, [data-question], [data-field-container]');
            if (group) {
              const directLabel = group.querySelector(':scope > label, :scope > .label, :scope > [data-label]');
              if (directLabel) parts.push(clean(directLabel.innerText || directLabel.textContent));
              const legend = group.querySelector('legend');
              if (legend) parts.push(clean(legend.innerText || legend.textContent));
            }
            const wrappingLabel = el.closest('label');
            if (wrappingLabel) parts.push(clean(wrappingLabel.innerText || wrappingLabel.textContent));
            return parts.find(Boolean) || clean(el.getAttribute('name')) || clean(el.getAttribute('placeholder'));
          };
          const optionLabelFor = el => {
            const parts = [];
            const id = el.getAttribute('id');
            if (id && window.CSS && CSS.escape) {
              document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(label => parts.push(clean(label.innerText || label.textContent)));
            }
            const wrappingLabel = el.closest('label');
            if (wrappingLabel) parts.push(clean(wrappingLabel.innerText || wrappingLabel.textContent));
            parts.push(clean(el.getAttribute('aria-label')));
            parts.push(clean(el.value));
            return parts.find(Boolean);
          };
          const validationFor = el => {
            const container = el.closest('fieldset, [role="group"], [role="radiogroup"], .form-group, .field, .question, [data-question], [data-field-container]') || el.parentElement;
            if (!container) return '';
            const messages = [];
            container.querySelectorAll('[role="alert"], .error, .validation-error, [data-validation], [data-error]').forEach(node => {
              if (visible(node)) messages.push(clean(node.innerText || node.textContent));
            });
            const describedBy = clean(el.getAttribute('aria-describedby'));
            if (describedBy) {
              describedBy.split(/\\s+/).forEach(idPart => {
                const text = byIdText(idPart);
                if (text) messages.push(text);
              });
            }
            return messages.find(Boolean) || '';
          };
          const requiredFor = el => {
            return !!(
              el.required ||
              el.getAttribute('aria-required') === 'true' ||
              el.getAttribute('data-required') === 'true' ||
              /\\*/.test(labelFor(el)) ||
              validationFor(el)
            );
          };
          const valuePresent = el => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'select') return !!el.value;
            if (el.type === 'radio') {
              const name = el.getAttribute('name');
              if (name && window.CSS && CSS.escape) {
                return !!document.querySelector(`input[type="radio"][name="${CSS.escape(name)}"]:checked`);
              }
            }
            if (el.type === 'checkbox') return el.checked;
            return !!clean(el.value);
          };
          const optionsFor = el => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'select') {
              return Array.from(el.options).map(option => clean(option.textContent)).filter(Boolean).filter(text => !/^select one$/i.test(text));
            }
            if (el.type === 'radio' || el.type === 'checkbox') {
              const group = el.closest('fieldset, [role="radiogroup"], [role="group"], .form-group, .question, [data-question]') || document;
              const selector = el.type === 'radio' && el.name && window.CSS && CSS.escape
                ? `input[type="radio"][name="${CSS.escape(el.name)}"]`
                : `input[type="${el.type}"]`;
              return Array.from(group.querySelectorAll(selector)).filter(visible).map(item => optionLabelFor(item)).filter(Boolean);
            }
            return [];
          };
          const controls = Array.from(document.querySelectorAll('input, select, textarea'))
            .filter(visible)
            .filter(el => !['button', 'submit', 'reset', 'hidden'].includes(String(el.type || '').toLowerCase()))
            .filter(requiredFor);
          const seen = new Set();
          const rows = [];
          for (const el of controls) {
            const type = String(el.type || el.tagName).toLowerCase();
            const controlType = el.tagName.toLowerCase() === 'select' ? 'select' : (el.tagName.toLowerCase() === 'textarea' ? 'textarea' : type);
            const rawText = labelFor(el);
            const dataQuestionNode = el.closest('[data-question]');
            if (!rawText) continue;
            const groupKey = controlType === 'radio' ? `${rawText}|radio|${el.name || ''}` : `${rawText}|${controlType}|${el.getAttribute('name') || el.id || rows.length}`;
            if (seen.has(groupKey)) continue;
            seen.add(groupKey);
            rows.push({
              raw_text: rawText,
              required: true,
              control_type: controlType,
              options: optionsFor(el),
              value_present: valuePresent(el),
              validation_message: validationFor(el),
              locator_hints: {
                tag: el.tagName.toLowerCase(),
                type,
                id: clean(el.getAttribute('id')),
                name: clean(el.getAttribute('name')),
                aria_label: clean(el.getAttribute('aria-label')),
                data_automation_id: clean(el.getAttribute('data-automation-id')),
                data_field: clean(el.getAttribute('data-field')),
                data_question: clean(dataQuestionNode ? dataQuestionNode.getAttribute('data-question') : '')
              }
            });
          }
          return rows;
        }"""
    )
    questions: list[DetectedQuestion] = []
    for row in rows:
        validation_message = row.get("validation_message") or ""
        has_blocking_validation = is_blocking_validation_message(validation_message)
        if not has_blocking_validation and (row.get("value_present") or validation_message):
            continue
        raw_text = row.get("raw_text") or ""
        normalized_text = normalize_question_text(raw_text)
        control_type = row.get("control_type") or "text"
        options = row.get("options") or []
        fingerprint = fingerprint_question(normalized_text, control_type, options)
        question = DetectedQuestion(
            raw_text=raw_text,
            normalized_text=normalized_text,
            fingerprint=fingerprint,
            required=bool(row.get("required")),
            control_type=control_type,
            options=options,
            validation_message=validation_message,
            locator_hints=row.get("locator_hints") or {},
            status=UNANSWERED,
        )
        approved = approved_answer_for(question, approved_answers)
        explicit_user_value = user_data_value_for_question(question, user_data)
        if (approved is not None or explicit_user_value is not None) and has_blocking_validation:
            question.status = TECHNICAL_REVIEW
        questions.append(question)
    return questions


def user_data_value_for_question(question: DetectedQuestion, user_data: dict[str, Any] | None) -> Any:
    data = user_data or {}
    text = question.normalized_text
    for key, value in data.items():
        key_norm = normalize_question_text(key)
        if value not in (None, "") and key_norm and (key_norm == text or key_norm in text or text in key_norm):
            return value
    return None


def outcome_status_for_questions(questions: list[DetectedQuestion]) -> str | None:
    if not questions:
        return None
    if any(question.status == TECHNICAL_REVIEW for question in questions):
        return NEEDS_TECHNICAL_REVIEW
    return "BLOCKED_ON_QUESTIONS"


def detector_questions_payload(questions: list[DetectedQuestion]) -> list[dict[str, Any]]:
    return [question.to_dict() for question in questions]
