from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts import ActionResult, FieldState, FieldStatus
from .base import BaseWorkdayWidget, WorkdayWidgetContext
from .utilities import (
    first_visible_locator,
    is_forbidden_submit_control,
    locator_tag,
    nearest_field_scope,
    normalize_for_match,
    safe_count,
    safe_input_value,
    safe_text,
    scoped_locator,
    upload_success_markers,
)


class WorkdayFileUploadWidget(BaseWorkdayWidget):
    upload_button_selectors = [
        'input[type="file"]',
        'button:has-text("Upload")',
        'button:has-text("Attach")',
        '[role="button"]:has-text("Upload")',
        '[role="button"]:has-text("Attach")',
        '[data-automation-id*="upload" i]',
        '[data-automation-id*="attachment" i]',
        ".dropzone",
    ]

    def locate(self, page: Any, context: Any | None = None) -> Any:
        ctx = WorkdayWidgetContext.from_any(context)
        locator = scoped_locator(page, ctx.selector, ctx.scope_index) if ctx.selector else None
        if locator is None:
            for selector in ctx.locator_hints:
                locator = scoped_locator(page, selector, ctx.scope_index)
                if locator is not None:
                    break
        if locator is None:
            locator = first_visible_locator(page, self.upload_button_selectors)
        if locator is None:
            raise LookupError("file upload control not found")
        return locator

    def upload(self, page: Any, path: str | Path, context: Any | None = None) -> ActionResult:
        return self.act(page, str(path), context)

    def act(self, page: Any, value: Any, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        before = self.observe(page, ctx)
        file_path = Path(str(value))
        try:
            locator = self.locate(page, ctx)
            input_locator = locator if self._is_file_input(locator) else locator.locator('input[type="file"]').first
            if safe_count(input_locator):
                input_locator.set_input_files(str(file_path), timeout=2500)
            else:
                if is_forbidden_submit_control(locator):
                    raise ValueError("refusing_to_click_submit_like_control")
                with page.expect_file_chooser(timeout=1500) as chooser_info:
                    locator.click(timeout=1500)
                chooser_info.value.set_files(str(file_path))
        except Exception as exc:
            after = self.observe(page, ctx)
            return self.result(
                acted=False,
                verified=False,
                action="upload_file",
                target=ctx.canonical_key,
                value=str(file_path),
                before=before,
                after=after,
                reason=f"upload_failed:{exc}",
                retryable=True,
            )
        self.wait_until_stable(page, ctx, self.default_timeout_ms)
        after = self.observe(page, ctx)
        verify = self.verify_upload(page, file_path.name, ctx)
        return self.result(
            acted=True,
            verified=verify.verified,
            action="upload_file",
            target=ctx.canonical_key,
            value=str(file_path),
            before=before,
            after=after,
            reason="verified" if verify.verified else verify.reason,
            retryable=not verify.verified,
        )

    def read_uploaded_files(self, page: Any, context: Any | None = None) -> list[str]:
        ctx = WorkdayWidgetContext.from_any(context)
        filenames: list[str] = []
        try:
            locator = self.locate(page, ctx)
            input_locator = locator if self._is_file_input(locator) else locator.locator('input[type="file"]').first
            if safe_count(input_locator):
                names = input_locator.evaluate("el => Array.from(el.files || []).map(file => file.name)")
                filenames.extend(str(item) for item in names)
        except Exception:
            pass
        filenames.extend(self._scoped_upload_markers(page, context=ctx))
        return list(dict.fromkeys(item for item in filenames if item))

    def verify_upload(self, page: Any, filename: str, context: Any | None = None) -> ActionResult:
        ctx = WorkdayWidgetContext.from_any(context)
        state = self.observe(page, ctx)
        markers = self._scoped_upload_markers(page, filename, ctx)
        verified = bool(markers)
        return self.result(
            acted=False,
            verified=verified,
            action="verify_upload",
            target=ctx.canonical_key,
            value=filename,
            after=state,
            reason="verified" if verified else "upload_not_committed",
            retryable=not verified,
            metadata={"markers": markers, "uploaded_files": self.read_uploaded_files(page, ctx)},
        )

    def verify(self, page: Any, expected_value: Any, context: Any | None = None) -> ActionResult:
        return self.verify_upload(page, Path(str(expected_value)).name, context)

    def observe(self, page: Any, context: Any | None = None) -> FieldState:
        ctx = WorkdayWidgetContext.from_any(context)
        files = self.read_uploaded_files(page, ctx)
        committed = self._scoped_upload_markers(page, context=ctx)
        status = FieldStatus.FILLED if committed else (FieldStatus.MISSING if ctx.required else FieldStatus.OPTIONAL)
        return FieldState(
            canonical_key=ctx.canonical_key,
            required=ctx.required,
            status=status,
            visible_value=committed or files,
            expected_value=ctx.expected_value,
            source=self.__class__.__name__,
            locator_hints=[ctx.selector, *ctx.locator_hints],
            metadata={"uploaded_files": files, "committed_markers": committed},
        )

    def _is_file_input(self, locator: Any) -> bool:
        try:
            return locator.evaluate("el => el.matches('input[type=\"file\"]')")
        except Exception:
            return False

    def _scoped_upload_markers(
        self,
        page: Any,
        filename: str = "",
        context: Any | None = None,
    ) -> list[str]:
        try:
            scope, broad_scope = self._upload_scope(page, context)
        except Exception:
            scope, broad_scope = page, True
        markers = upload_success_markers(scope, filename)
        if broad_scope:
            if not filename:
                return []
            markers = [marker for marker in markers if self._marker_mentions_filename(marker, filename)]
        return markers

    def _upload_scope(self, page: Any, context: Any | None = None) -> tuple[Any, bool]:
        locator = self.locate(page, context)
        scope = nearest_field_scope(locator)
        tag = locator_tag(scope)
        broad_scope = tag in {"body", "html"}
        return scope, broad_scope

    def _marker_mentions_filename(self, marker: str, filename: str) -> bool:
        filename_norm = normalize_for_match(Path(filename).name)
        marker_norm = normalize_for_match(marker)
        return bool(filename_norm and filename_norm in marker_norm)
