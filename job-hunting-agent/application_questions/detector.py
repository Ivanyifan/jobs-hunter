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
    r"\bexport control\b",
    r"\bcitizenship\b",
    r"\bpermanent resident\b",
    r"\bgovernment official\b",
    r"\belectronic signature\b",
]

NON_BLOCKING_VALIDATION_PATTERNS = [
    r"\b\d+\s+item[s]?\s+selected\b",
    r"\bpress delete to clear value\b",
    r"^alert:\s*verify that the field\b.*\bcorrectly capitalized\b",
    r"^current value is\s+(?!(mm\s*/?\s*yyyy|yyyy|mm|month|year|select one)\b).+",
]

PLACEHOLDER_QUESTION_RE = re.compile(
    r"^(select|select one|choose|choose one|choose an answer|choose an option|required|"
    r"select one required|month|day|year|mm|dd|yyyy|current value is (mm/dd/yyyy|mm/yyyy|yyyy))$",
    re.IGNORECASE,
)

WORKDAY_VALIDATION_FIELD_RE = re.compile(
    r"^(?:error\s*)?(?:the field\s+)?(.+?)\s+is required and must have a value\.?$",
    re.IGNORECASE,
)

QUESTION_CANONICAL_ALIAS_PATTERNS = {
    "how_heard": [
        r"\bhow\b.*\bhear\b.*\babout\b.*\bus\b",
        r"\bhow\b.*\bdid\b.*\byou\b.*\bhear\b",
        r"\bsource\b.*\bapplication\b",
    ],
    "authorized_to_work_us": [
        r"\blegally authorized\b.*\bwork\b",
        r"\bauthorized\b.*\bwork\b",
        r"\bwork authorization\b",
    ],
    "need_sponsorship": [
        r"\brequire sponsorship\b",
        r"\bfuture\b.*\bsponsor",
        r"\bnow\b.*\bfuture\b.*\bsponsor",
        r"\bvisa\b.*\bsponsor",
    ],
    "conflict_of_interest": [
        r"\bconflict of interest\b",
        r"\boutside employment\b.*\bcompetitor\b",
        r"\bsignificant financial interest\b",
        r"\bgovernment official\b.*\bconflict\b",
    ],
    "export_control": [
        r"\bexport control\b",
        r"\bcitizen\b.*\bpermanent resident\b",
        r"\bcitizenship/permanent residency\b",
    ],
    "current_or_previous_company_employee": [
        r"\bexisting\b.*\bemployee\b",
        r"\bcurrent\b.*\bemployee\b",
        r"\bprevious\b.*\bemployee\b",
        r"\bformerly\b.*\bemployed\b",
    ],
    "start_date": [
        r"\bwhen\b.*\bavailable\b.*\bstart\b",
        r"\bavailable\b.*\bstart\b",
        r"\bearliest\b.*\bstart\b",
        r"\bstart date\b",
    ],
    "phone_device_type": [
        r"\bphone device type\b",
        r"\bphone type\b",
        r"\bdevice type\b",
    ],
    "phone_number": [
        r"\bphone number\b",
        r"\bmobile number\b",
        r"\bcell(?:ular)? number\b",
    ],
    "postal_code": [
        r"\bpostal code\b",
        r"\bzip code\b",
        r"\bzip\b",
    ],
    "country": [
        r"\bresidence country\b",
        r"\bcountry/region\b",
        r"\bcountry region\b",
        r"^country\*?$",
    ],
    "state": [
        r"\bstate or territory\b",
        r"\bstate\b",
        r"\bprovince\b",
        r"\bregion\b",
    ],
    "city": [
        r"^city\*?$",
        r"\bsuburb\b",
        r"\blocality\b",
    ],
}

SUPPORTED_SEMANTIC_CANONICAL_KEYS = {
    "how_heard",
    "current_or_previous_company_employee",
    "authorized_to_work_us",
    "need_sponsorship",
    "conflict_of_interest",
    "export_control",
    "start_date",
    "phone_device_type",
    "phone_number",
    "postal_code",
    "country",
    "state",
    "city",
}

SENSITIVE_COMPLIANCE_CANONICAL_KEYS = {
    "authorized_to_work_us",
    "need_sponsorship",
    "conflict_of_interest",
    "export_control",
    "current_or_previous_company_employee",
}

SEMANTIC_RISK_LEVELS = {"low", "medium", "sensitive_compliance"}
SEMANTIC_CLASSIFIER_CONFIDENCE_THRESHOLD = 0.85


def is_sensitive_question(text: str | None) -> bool:
    lowered = str(text or "").lower()
    return any(re.search(pattern, lowered) for pattern in SENSITIVE_PATTERNS)


def semantic_risk_for_canonical_key(canonical_key: str | None, text: str | None = None) -> str:
    if canonical_key in SENSITIVE_COMPLIANCE_CANONICAL_KEYS or is_sensitive_question(text):
        return "sensitive_compliance"
    if canonical_key in {"how_heard", "phone_device_type", "phone_number", "postal_code", "country", "state", "city", "start_date"}:
        return "low"
    return "medium"


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


def clean_question_candidate(text: str | None) -> str:
    value = str(text or "").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return ""
    value = re.sub(r"^error\s*", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"\b(select one|choose one|choose an answer|choose an option|required)\b", " ", value, flags=re.IGNORECASE)
    date_placeholder = bool(re.search(r"\b(current value is|mm|dd|yyyy)\b", value, re.IGNORECASE))
    value = re.sub(r"\bcurrent value is\s+(mm\s*/\s*dd\s*/\s*yyyy|mm\s*/\s*yyyy|yyyy)\b", " ", value, flags=re.IGNORECASE)
    if date_placeholder:
        value = re.sub(r"\b(mm|dd|yyyy|month|day|year)\b\s*/?\s*", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip(" :;,.*")


def question_text_from_validation(text: str | None) -> str:
    value = str(text or "").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    validation_match = WORKDAY_VALIDATION_FIELD_RE.match(value)
    return validation_match.group(1).strip(" :;,.*") if validation_match else ""


def placeholder_question_text(text: str | None) -> bool:
    value = normalize_question_text(text)
    if not value:
        return True
    return bool(PLACEHOLDER_QUESTION_RE.match(value))


def canonical_key_for_question(text: str | None, context: dict[str, Any] | None = None) -> str | None:
    context = context or {}
    haystack = " ".join([
        str(text or ""),
        str(context.get("validation_message") or ""),
        str(context.get("nearest_group_text") or ""),
        str(context.get("fieldset_legend") or ""),
        str(context.get("aria_labelledby_text") or ""),
        str(context.get("label_for_text") or ""),
        str(context.get("preceding_sibling_text") or ""),
    ])
    for key, patterns in QUESTION_CANONICAL_ALIAS_PATTERNS.items():
        if any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns):
            return key
    return None


def llm_semantic_question_classifier(context: dict[str, Any], user_data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    classifier = (user_data or {}).get("llm_question_canonicalizer") if isinstance(user_data, dict) else None
    if callable(classifier):
        try:
            result = classifier(context)
            if not isinstance(result, dict):
                return None
            sanitized = dict(result)
            for forbidden in ["answer", "safe_to_autofill", "selected_answer", "value"]:
                sanitized.pop(forbidden, None)
            canonical_key = sanitized.get("canonical_key")
            if canonical_key not in SUPPORTED_SEMANTIC_CANONICAL_KEYS:
                sanitized["canonical_key"] = None
            risk_level = sanitized.get("risk_level")
            if risk_level not in SEMANTIC_RISK_LEVELS:
                sanitized["risk_level"] = semantic_risk_for_canonical_key(
                    sanitized.get("canonical_key"),
                    sanitized.get("question_text") or context.get("nearest_group_text"),
                )
            try:
                sanitized["confidence"] = float(sanitized.get("confidence") or 0.0)
            except Exception:
                sanitized["confidence"] = 0.0
            return sanitized
        except Exception:
            return None
    return None


def semantic_question_text_from_context(row: dict[str, Any], user_data: dict[str, Any] | None = None) -> tuple[str, str | None, dict[str, Any]]:
    context = dict(row.get("question_context") or {})
    validation = row.get("validation_message") or context.get("validation_message") or ""
    if validation:
        context["validation_message"] = validation
    context.setdefault("options", row.get("options") or [])
    raw_text = str(row.get("raw_text") or "")

    validation_question = question_text_from_validation(validation)
    candidates = [
        validation_question,
        context.get("fieldset_legend"),
        context.get("label_for_text"),
        context.get("aria_labelledby_text"),
        context.get("nearest_group_text"),
        context.get("preceding_sibling_text"),
        raw_text,
    ]
    chosen = ""
    for candidate in candidates:
        cleaned = clean_question_candidate(candidate)
        if cleaned and not placeholder_question_text(cleaned):
            chosen = cleaned
            break
    if not chosen:
        chosen = clean_question_candidate(raw_text) or raw_text.strip()

    canonical_key = canonical_key_for_question(chosen, context)
    ambiguous = (
        placeholder_question_text(chosen)
        or (not canonical_key and placeholder_question_text(raw_text))
        or (not canonical_key and bool(context.get("options")))
    )
    if ambiguous:
        llm_result = llm_semantic_question_classifier(context, user_data)
        if isinstance(llm_result, dict):
            llm_text = clean_question_candidate(llm_result.get("question_text"))
            llm_key = llm_result.get("canonical_key")
            if llm_text and not placeholder_question_text(llm_text):
                chosen = llm_text
            if llm_key:
                canonical_key = str(llm_key)
            context["semantic_classifier"] = {
                "used": True,
                "canonical_key": canonical_key,
                "risk_level": llm_result.get("risk_level"),
                "confidence": llm_result.get("confidence"),
                "evidence": llm_result.get("evidence"),
            }
            if float(llm_result.get("confidence") or 0.0) < SEMANTIC_CLASSIFIER_CONFIDENCE_THRESHOLD:
                context["semantic_classifier"]["requires_technical_review"] = True
                context["semantic_classifier"]["reason"] = "semantic_classifier_low_confidence"
    return chosen, canonical_key, context


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
          const byIdsText = ids => clean(String(ids || '').split(/\\s+/).map(byIdText).filter(Boolean).join(' '));
          const isSelectLike = el => {
            const tag = el.tagName.toLowerCase();
            const role = String(el.getAttribute('role') || '').toLowerCase();
            const popup = String(el.getAttribute('aria-haspopup') || '').toLowerCase();
            const automation = String(el.getAttribute('data-automation-id') || '').toLowerCase();
            return tag === 'select' || role === 'combobox' || popup === 'listbox' ||
              el.hasAttribute('aria-expanded') || /prompt|select|dropdown|combobox/.test(automation);
          };
          const placeholderSelectText = text => /^(select|select one|select one required|choose|choose one|choose an answer|choose an option|search|required)?$/i.test(clean(text));
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
            const group = el.closest('[role="group"], [role="radiogroup"], .form-group, .field, .question, [data-question], [data-field-container], li, section');
            if (group) {
              const directLabel = group.querySelector(':scope > label, :scope > .label, :scope > [data-label]');
              if (directLabel) parts.push(clean(directLabel.innerText || directLabel.textContent));
              const legend = group.querySelector('legend');
              if (legend) parts.push(clean(legend.innerText || legend.textContent));
              const groupText = clean(group.innerText || group.textContent);
              if (groupText && groupText.length < 1600) parts.push(groupText);
            }
            const wrappingLabel = el.closest('label');
            if (wrappingLabel) parts.push(clean(wrappingLabel.innerText || wrappingLabel.textContent));
            return parts.find(Boolean) || clean(el.getAttribute('name')) || clean(el.getAttribute('placeholder'));
          };
          const groupFor = el => el.closest('fieldset, [role="group"], [role="radiogroup"], .form-group, .field, .question, [data-question], [data-field-container], li, section') || el.parentElement;
          const labelForAttributeText = el => {
            const id = el.getAttribute('id');
            if (!id || !window.CSS || !CSS.escape) return '';
            const labels = Array.from(document.querySelectorAll(`label[for="${CSS.escape(id)}"]`))
              .map(label => clean(label.innerText || label.textContent))
              .filter(Boolean);
            return labels.join(' ');
          };
          const fieldsetLegendText = el => {
            const fieldset = el.closest('fieldset');
            if (!fieldset) return '';
            const legend = fieldset.querySelector('legend');
            return legend ? clean(legend.innerText || legend.textContent) : '';
          };
          const nearestGroupText = el => {
            const group = groupFor(el);
            if (!group) return '';
            const clone = group.cloneNode(true);
            clone.querySelectorAll('script, style, option, [role="option"]').forEach(node => node.remove());
            return clean(clone.innerText || clone.textContent).slice(0, 1600);
          };
          const precedingSiblingText = el => {
            const parts = [];
            let node = el.previousElementSibling;
            let count = 0;
            while (node && count < 4) {
              const text = clean(node.innerText || node.textContent);
              if (text) parts.unshift(text);
              node = node.previousElementSibling;
              count += 1;
            }
            return clean(parts.join(' ')).slice(0, 800);
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
            if (isSelectLike(el)) {
              const text = clean(el.value || el.innerText || el.textContent || el.getAttribute('aria-label') || '');
              return !!text && !placeholderSelectText(text);
            }
            if (el.type === 'radio') {
              const name = el.getAttribute('name');
              if (name && window.CSS && CSS.escape) {
                return !!document.querySelector(`input[type="radio"][name="${CSS.escape(name)}"]:checked`);
              }
            }
            if (el.type === 'checkbox') return el.checked;
            return !!clean(el.value);
          };
          const visibleValueFor = el => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'select') {
              const option = el.selectedOptions && el.selectedOptions[0];
              return clean((option && option.textContent) || el.value);
            }
            if (el.type === 'checkbox' || el.type === 'radio') {
              return el.checked ? optionLabelFor(el) : '';
            }
            return clean(el.value || el.innerText || el.textContent || el.getAttribute('aria-label') || '');
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
            if (isSelectLike(el)) {
              const ids = [
                el.getAttribute('aria-controls'),
                el.getAttribute('aria-owns'),
                el.getAttribute('aria-describedby')
              ].filter(Boolean).join(' ');
              const options = [];
              ids.split(/\\s+/).forEach(idPart => {
                const node = document.getElementById(idPart);
                if (!node) return;
                node.querySelectorAll('[role="option"], li, option').forEach(option => {
                  const text = clean(option.innerText || option.textContent);
                  if (text && !/^select one$/i.test(text)) options.push(text);
                });
              });
              const group = groupFor(el);
              if (group) {
                group.querySelectorAll('[role="option"], option').forEach(option => {
                  const text = clean(option.innerText || option.textContent);
                  if (text && !/^select one$/i.test(text)) options.push(text);
                });
              }
              return Array.from(new Set(options)).slice(0, 40);
            }
            return [];
          };
          const currentStage = () => {
            const text = clean(document.body ? (document.body.innerText || document.body.textContent) : '');
            const match = text.match(/\\b(My Information|My Experience|Application Questions(?:\\s+\\d+\\s+of\\s+\\d+)?|Voluntary Disclosures|Self Identify|Review)\\b/i);
            return match ? match[0] : '';
          };
          const controls = Array.from(document.querySelectorAll('input, select, textarea, button[aria-haspopup], button[aria-expanded], [role="combobox"]'))
            .filter(visible)
            .filter(el => isSelectLike(el) || !['button', 'submit', 'reset', 'hidden'].includes(String(el.type || '').toLowerCase()))
            .filter(requiredFor);
          const seen = new Set();
          const rows = [];
          for (const el of controls) {
            const type = String(el.type || el.tagName).toLowerCase();
            const controlType = isSelectLike(el) ? 'select' : (el.tagName.toLowerCase() === 'textarea' ? 'textarea' : type);
            const rawText = labelFor(el);
            const dataQuestionNode = el.closest('[data-question]');
            const options = optionsFor(el);
            if (!rawText) continue;
            const groupKey = controlType === 'radio' ? `${rawText}|radio|${el.name || ''}` : `${rawText}|${controlType}|${el.getAttribute('name') || el.id || rows.length}`;
            if (seen.has(groupKey)) continue;
            seen.add(groupKey);
            rows.push({
              raw_text: rawText,
              required: true,
              control_type: controlType,
              options,
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
              },
              question_context: {
                aria_labelledby_text: byIdsText(el.getAttribute('aria-labelledby')),
                field_label: rawText,
                label_for_text: labelForAttributeText(el),
                fieldset_legend: fieldsetLegendText(el),
                nearest_group_text: nearestGroupText(el),
                validation_message: validationFor(el),
                preceding_sibling_text: precedingSiblingText(el),
                options,
                current_stage: currentStage(),
                current_visible_value: visibleValueFor(el)
              }
            });
          }
          return rows;
        }"""
    )
    questions: list[DetectedQuestion] = []
    seen_questions: set[tuple[str, str, str]] = set()
    for row in rows:
        validation_message = row.get("validation_message") or ""
        has_blocking_validation = is_blocking_validation_message(validation_message)
        if not has_blocking_validation and (row.get("value_present") or validation_message):
            continue
        raw_text, canonical_key, question_context = semantic_question_text_from_context(row, user_data)
        if not raw_text:
            continue
        normalized_text = normalize_question_text(raw_text)
        control_type = row.get("control_type") or "text"
        options = row.get("options") or []
        semantic_locator = "" if canonical_key == "start_date" else str(
            (row.get("locator_hints") or {}).get("name") or (row.get("locator_hints") or {}).get("id") or ""
        )
        semantic_key = (normalized_text, control_type, semantic_locator)
        if semantic_key in seen_questions:
            continue
        seen_questions.add(semantic_key)
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
            canonical_key=canonical_key,
            question_context=question_context,
        )
        if (question_context.get("semantic_classifier") or {}).get("requires_technical_review"):
            question.status = TECHNICAL_REVIEW
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
