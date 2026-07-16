import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frontend.apply_flow import build_resume_tailoring_prompt, build_skill_match_report
from frontend.scheduler_worker import ScheduledApplyWorker


class CapturingModels:
    def __init__(self):
        self.prompts = []

    def generate_content(self, **kwargs):
        self.prompts.append(kwargs["contents"])
        return SimpleNamespace(text=(
            "# Candidate\n\nSummary\nPython backend engineer.\n\n"
            "Experience\nBuilt reliable Python services and production APIs.\n\n"
            "Skills\nPython, FastAPI\n" + ("Reliable systems engineering.\n" * 8)
        ))


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"passed": True, "trace_id": "trace-1"}


class SchedulerSkillLibraryTests(unittest.TestCase):
    def make_worker(self):
        return object.__new__(ScheduledApplyWorker)

    def test_scheduler_schema_and_save_include_match_scores_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = self.make_worker()
            worker.sqlite_db_path = str(Path(temp_dir) / "applications.db")
            worker.init_db()
            match_scores = {
                "v0": {"match_score": 0.4},
                "v1": {"match_score": 0.7},
                "skill_library_match": {"jd_matched_skills": ["FastAPI"]},
            }
            worker.save_local_application(
                "app-1",
                "Example",
                "Backend Engineer",
                "Python",
                "Python, FastAPI",
                "https://example.test/job",
                match_scores,
            )

            conn = sqlite3.connect(worker.sqlite_db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(mcp_applications)")}
                row = conn.execute(
                    "SELECT match_scores_json FROM mcp_applications WHERE id = ?",
                    ("app-1",),
                ).fetchone()
            finally:
                conn.close()

        self.assertIn("match_scores_json", columns)
        self.assertEqual(json.loads(row[0]), match_scores)

    def test_scheduler_uses_shared_tailoring_prompt_and_excludes_unmatched_skills(self):
        worker = self.make_worker()
        models = CapturingModels()
        client = SimpleNamespace(models=models)
        resume_v0 = "Skills\nPython\nExperience\nBuilt Python APIs."
        job_description = "Python and FastAPI are required."
        skills = ["Python", "FastAPI", "React"]

        result, error = worker.tailor_resume_for_job(
            client,
            "gemini-test",
            resume_v0,
            job_description,
            "Example",
            "Backend Engineer",
            skill_library=skills,
        )
        report = build_skill_match_report(job_description, resume_v0, skills)
        expected_prompt = build_resume_tailoring_prompt(
            resume_v0,
            job_description,
            "Example",
            "Backend Engineer",
            skill_match=report,
            one_page=True,
        )

        self.assertIsNone(error)
        self.assertGreater(len(result), 200)
        self.assertEqual(models.prompts[0], expected_prompt)
        self.assertIn("FastAPI", models.prompts[0])
        self.assertNotIn("React", models.prompts[0])

    def test_scheduler_arize_payload_contains_trusted_skills(self):
        worker = self.make_worker()
        worker.sync_mongo_artifact = lambda *args, **kwargs: None
        worker.sync_mongo_event = lambda *args, **kwargs: None
        config = {
            "arize_url": "http://arize.test",
            "user_data": {
                "application_profile_library": {
                    "skills": ["Python", "FastAPI"],
                }
            },
        }

        with patch("frontend.scheduler_worker.requests.post", return_value=FakeResponse()) as post:
            result = worker.call_arize_audit(
                config,
                "app-1",
                "Example",
                "Backend Engineer",
                "Python",
                "Python, FastAPI",
            )

        self.assertTrue(result["passed"])
        self.assertEqual(post.call_args.kwargs["json"]["trusted_skills"], ["Python", "FastAPI"])


if __name__ == "__main__":
    unittest.main()
