# Job Hunter Agent

Evidence-based job applications, tailored safely for every role.

Job Hunter Agent is a multi-service application assistant that helps candidates discover jobs, tailor resumes, audit resume quality, inspect external application forms, and stop before risky or irreversible actions.

The main project lives in [`job-hunting-agent/`](job-hunting-agent/).

## Highlights

- Per-job resume-to-JD fit scoring for `V0` versus `V1`.
- Structure-first external application precheck using DOM, accessibility data, form schema, and stable locators.
- Vision-assisted fallback for difficult browser states.
- Human confirmation gates for sensitive fields and final submission.
- MongoDB-backed application memory for jobs, events, artifacts, resume versions, and outcomes.
- SOMA retrieval for similar historical application episodes and reusable rewrite patterns.
- Cloud Run deployment scripts for the frontend and tool services.

## Tech Stack

Python, JavaScript, HTML/CSS, YAML, Markdown, FastAPI, Streamlit, Playwright, Pydantic, PyMongo, Elasticsearch, Google GenAI SDK, Gemini API, PyMuPDF, pypdf, Google Cloud Run, Google Cloud Build, Google Artifact Registry, Google Secret Manager, Google Cloud VPC, Cloud NAT, Arize Phoenix, MongoDB Atlas, SQLite, Gmail IMAP, LinkedIn, Workday, BrassRing, Greenhouse, Lever, Docker, OpenAPI, Chrome Extension.

## License

This repository is released under the [MIT License](LICENSE), an OSI-approved license.
