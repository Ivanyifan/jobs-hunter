# Direct Cloud Run Deployment

This is the fastest cloud path for the hackathon demo. It deploys each local tool server as a separate Cloud Run service and wires the Streamlit UI to those service URLs.

## What This Deploys

| Cloud Run service | Local equivalent | Command |
| --- | --- | --- |
| `jobs-mongo` | `8001` | `python mcp_servers/mongo_server.py` |
| `jobs-elastic` | `8002` | `python mcp_servers/elastic_server.py` |
| `jobs-arize` | `8003` | `python mcp_servers/arize_server.py` |
| `jobs-email` | `8005` | `python mcp_servers/email_server.py` |
| `jobs-playwright` | `8004` | `python mcp_servers/playwright_server.py` |
| `jobs-frontend` | `8501` | `python cloudrun/run_streamlit.py` |

## Why Static Egress Is Included

MongoDB Atlas is blocking the current local network before TLS completes. Cloud Run does not have a stable outbound IP by default, so the deployment script creates:

- a custom VPC
- a subnet
- a reserved regional static external IP
- a Cloud Router
- Cloud NAT
- Cloud Run direct VPC egress for all services

The script prints the static egress IP and pauses. Add that IP as `/32` in MongoDB Atlas Network Access before continuing.

## Before Running

Install and authenticate Google Cloud CLI, or run from Google Cloud Shell.

Set the required values in local environment variables or in `.env.local`. The deploy script can copy them into Secret Manager when `-BootstrapSecrets` is passed.

Required:

```powershell
$env:MONGO_URI = "mongodb+srv://..."
```

Recommended for full demo:

```powershell
$env:GEMINI_API_KEY = "..."
$env:ELASTIC_URL = "https://..."
$env:ELASTIC_API_KEY = "..."
$env:PHOENIX_COLLECTOR_ENDPOINT = "https://..."
$env:PHOENIX_API_KEY = "..."
$env:PHOENIX_PROJECT_NAME = "job-hunter-agent"
$env:EMAIL_IMAP_SERVER = "imap.gmail.com"
$env:EMAIL_USERNAME = "..."
$env:EMAIL_PASSWORD = "..."
$env:ATS_ACCOUNT_PASSWORD = "..."
```

Do not commit `.env.local`.

## Deploy

From the repo root:

```powershell
.\cloudrun\deploy_cloud_run.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -BootstrapSecrets
```

When prompted, copy the printed static outbound IP into:

```text
MongoDB Atlas -> Network Access -> Add IP Address -> <printed-ip>/32
```

Then press Enter in the terminal to continue.

## Validate

The script prints:

- frontend URL
- Mongo health URL
- Elastic health URL
- Arize health URL
- Atlas allowlist IP

Open the frontend URL and check:

1. Mongo memory panel loads without the `mongodb_unavailable` warning.
2. Elastic/SOMA retrieval health passes.
3. Arize audit traces can be created.
4. Playwright can run external apply precheck to a safe stop point.

## Known Shortcut For Today

Back-end Cloud Run services are deployed with `--allow-unauthenticated` so the Streamlit frontend can call them immediately. This is acceptable for a hackathon demo if the URLs are not publicly shared. The production version should add service-to-service authentication and only expose `jobs-frontend`.
