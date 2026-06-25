from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.workday.browser import launch_replay_browser
from adapters.workday.widgets.file_upload import WorkdayFileUploadWidget


class WorkdayFileUploadWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = launch_replay_browser(cls.playwright, headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 900, "height": 700})
        self.page = self.context.new_page()
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        temp.write(b"%PDF-1.4\n% widget test\n")
        temp.close()
        self.upload_path = Path(temp.name)

    def tearDown(self) -> None:
        self.context.close()
        self.upload_path.unlink(missing_ok=True)

    def set_content(self, body: str) -> None:
        self.page.set_content(f"<!doctype html><html><body>{body}</body></html>", wait_until="domcontentloaded")

    def test_set_input_files_without_marker_or_filename_is_not_verified_success(self):
        self.set_content('<input id="resume" type="file">')

        result = WorkdayFileUploadWidget().upload(
            self.page,
            self.upload_path,
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "upload_not_committed")

    def test_successfully_uploaded_or_filename_tile_is_verified_success(self):
        self.set_content(
            """
            <input id="resume" type="file">
            <div id="status" data-automation-id="uploadStatus"></div>
            <script>
              document.querySelector("#resume").addEventListener("change", event => {
                document.querySelector("#status").textContent = "Successfully Uploaded " + event.target.files[0].name;
              });
            </script>
            """
        )

        result = WorkdayFileUploadWidget().upload(
            self.page,
            self.upload_path,
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())

    def test_generic_upload_marker_added_by_current_action_verifies_success(self):
        self.set_content(
            """
            <section id="resume-field" data-field>
              <input id="resume" type="file">
              <div id="status" data-automation-id="uploadStatus"></div>
            </section>
            <script>
              document.querySelector("#resume").addEventListener("change", () => {
                document.querySelector("#status").textContent = "Successfully Uploaded";
              });
            </script>
            """
        )

        result = WorkdayFileUploadWidget().upload(
            self.page,
            self.upload_path,
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertTrue(result.verified, result.to_dict())
        self.assertEqual(result.metadata["new_generic_markers"], ["Successfully Uploaded"])

    def test_stale_generic_upload_marker_does_not_verify_filename(self):
        self.set_content(
            """
            <section id="resume-field" data-field>
              <input id="resume" type="file">
              <div data-automation-id="uploadStatus">Successfully Uploaded</div>
            </section>
            """
        )

        result = WorkdayFileUploadWidget().verify_upload(
            self.page,
            "resume.pdf",
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "upload_not_committed")

    def test_stale_old_filename_marker_does_not_verify_new_filename(self):
        self.set_content(
            """
            <section id="resume-field" data-field>
              <input id="resume" type="file">
              <div data-automation-id="uploadStatus">Successfully Uploaded old-resume.pdf</div>
            </section>
            """
        )

        result = WorkdayFileUploadWidget().verify_upload(
            self.page,
            "new-resume.pdf",
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "upload_not_committed")

    def test_other_upload_area_success_does_not_verify_current_upload(self):
        self.set_content(
            """
            <section id="cover-letter" data-field>
              <input id="cover" type="file">
              <div data-automation-id="uploadStatus">Successfully Uploaded cover-letter.pdf</div>
            </section>
            <section id="resume-field" data-field>
              <input id="resume" type="file">
            </section>
            """
        )

        result = WorkdayFileUploadWidget().verify_upload(
            self.page,
            "resume.pdf",
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "upload_not_committed")

    def test_final_submit_is_never_clicked(self):
        self.set_content(
            """
            <input id="resume" type="file">
            <button id="submit" type="submit">Submit</button>
            <script>
              window.submitClicks = 0;
              document.querySelector("#submit").addEventListener("click", () => window.submitClicks += 1);
            </script>
            """
        )

        WorkdayFileUploadWidget().upload(
            self.page,
            self.upload_path,
            {"selector": "#resume", "canonical_key": "resume_upload", "required": True},
        )

        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)


if __name__ == "__main__":
    unittest.main()
