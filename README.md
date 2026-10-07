# Jenkins MCP Server

## Overview

The Jenkins MCP Server acts as a bridge between Atlassian Rovo and Jenkins.

It enables users to retrieve Jenkins information through natural language queries in Jira using a Rovo Agent. The server communicates with Jenkins through Jenkins REST APIs and exposes MCP-compatible endpoints for Atlassian Rovo.

Supported use cases include:

- View Jenkins job status
- Retrieve failed jobs
- Analyze build failures
- View Jenkins metrics and health information
- Retrieve jobs from specific Jenkins views
- Identify long-running builds
- Provide Jenkins operational insights

---

## Architecture

```text
User
   ↓
Atlassian Rovo Agent
   ↓
Jenkins MCP Server
   ↓
Jenkins REST APIs
   ↓
Jenkins Data
```

---

## Project Structure

### app/auth.py

Handles:

- Dynamic Client Registration (DCR)
- OAuth token generation
- Client registration
- Access token management

Endpoints:

```text
POST /register
POST /token
```

---

### app/main.py

FastAPI application exposing:

```text
/register
/token
/mcp
```

Also exposes Jenkins helper APIs.

---

### app/tools.py

Contains business logic for:

- Job status retrieval
- Failed job information
- Failure analysis
- Jenkins metrics
- View-based job retrieval
- Long-running job analysis

---

### app/jenkins_client.py

Handles communication with Jenkins using Jenkins REST APIs.

---

### app/config.py

Loads Jenkins configuration values from the `.env` file.

---

### mcp_server.py

Standalone MCP tool host using:

```python
mcp.server.fastmcp
```

---

## Authentication Flow

The MCP Server uses Dynamic Client Registration (DCR) and OAuth authentication.

### Step 1: Dynamic Client Registration

Rovo registers itself by calling:

```http
POST /oauth/register
```

The server returns:

```json
{
  "client_id": "xxxxx",
  "client_secret": "xxxxx"
}
```

---

### Step 2: OAuth Token Generation

Rovo calls:

```http
POST /oauth/token
```

using the registered:

```text
client_id
client_secret
```

The server returns:

```json
{
  "access_token": "xxxxx",
  "token_type": "Bearer"
}
```

---

### Step 3: Access MCP Endpoint

Rovo calls:

```http
POST /mcp
```

with:

```text
Authorization: Bearer <access_token>
```

Only authenticated requests are allowed to access MCP tools.

Production configuration requires the following values from the approved secrets
manager or protected environment configuration:

```text
JENKINS_URL=https://jenkins.example.com
JENKINS_USER=<jenkins-service-user>
JENKINS_TOKEN=<jenkins-api-token>
ALLOWED_OAUTH_REDIRECT_URIS=https://id.atlassian.com/outboundAuth/finish
```

`JENKINS_URL` must use HTTPS. The service only sends Jenkins credentials to this
configured origin and validates its certificate using the operating system trust store.
Public REST wrappers require a bearer token, and production
does not expose `/docs`, `/redoc`, `/openapi.json`, or `/tools`.

Optional response-size and performance settings:

```text
MAX_BUILDS_PER_JOB=10
MAX_CONSOLE_LOG_BYTES=262144
JOBS_CACHE_TTL_SECONDS=300
JENKINS_MAX_WORKERS=8
JENKINS_DISCOVERY_TIMEOUT_SECONDS=20
JENKINS_FETCH_TIMEOUT_SECONDS=25
JENKINS_SKELETON_MAX_DEPTH=6
REQUEST_TIMEOUT=10
JOBS_REFRESH_INTERVAL_SECONDS=300
BACKGROUND_DISCOVERY_TIMEOUT_SECONDS=300
FUZZY_MIN_SCORE=60
```

`MAX_BUILDS_PER_JOB` is the default history result limit per job; an explicit
larger `page_size` request overrides that default. `MAX_CONSOLE_LOG_BYTES`
limits console text passed to failure analysis. Folder filters on MCP tools
scope work to one top-level Jenkins folder; without a filter, tools retain
their Jenkins-wide behavior.

`REQUEST_TIMEOUT` bounds individual Jenkins API calls. `JOBS_CACHE_TTL_SECONDS`
controls how long raw discovery results are cached. `JOBS_REFRESH_INTERVAL_SECONDS`
sets how often the background warmer refreshes the job index.
`BACKGROUND_DISCOVERY_TIMEOUT_SECONDS` gives the background walk a larger budget
than user-facing requests (no user is waiting). `FUZZY_MIN_SCORE` sets the
minimum score for fuzzy matching (0-100).

Job discovery uses a small number of deep Jenkins `tree` queries instead of
one HTTP request per folder plus a second request per job: each request asks
Jenkins to resolve up to `JENKINS_SKELETON_MAX_DEPTH` folder levels, along
with each job's `lastBuild` and up to `MAX_BUILDS_PER_JOB` recent builds, in a
single server-side response. Folders still unresolved at that depth are
queued for a follow-up query, run concurrently (bounded by
`JENKINS_MAX_WORKERS`). `get_all_jobs`, `get_jenkins_metrics`, and
`get_long_running_jobs` read status/build data directly from that discovery
result and only fall back to a live per-job request for entries the query
didn't resolve (for example, some multibranch branch listings).

`JENKINS_DISCOVERY_TIMEOUT_SECONDS` bounds how long the whole discovery walk
may run before returning partial results, and `JENKINS_FETCH_TIMEOUT_SECONDS`
bounds the live per-job fallback fetch phase. When either budget is exceeded,
`get_all_jobs`, `get_failed_jobs`, `analyze_failures`, `get_jenkins_metrics`,
`get_long_running_jobs`, and wildcard `get_build_history` calls return
`"truncated": true` with the counts of jobs discovered/processed so far,
instead of hanging until the caller's own timeout is reached. Truncated
discovery results are not cached, so the next call retries a full walk.

A background thread refreshes the job index every `JOBS_REFRESH_INTERVAL_SECONDS`.
This index is an immutable snapshot of all jobs, folders, and views, built
with fuzzy-matching metadata (display names, normalized tokens). User requests
read this snapshot atomically, so they never block on a live Jenkins walk.
The background walk uses the larger `BACKGROUND_DISCOVERY_TIMEOUT_SECONDS`
budget to ensure complete discovery even on large instances.

---

## Streamable HTTP Support

The MCP endpoint returns streamable responses.

Endpoint:

```http
POST /mcp
```

Response type:

```text
text/event-stream
```

The server streams progress messages and the final result.

---

## Available MCP Tools

### get_all_jobs_status

Retrieves the status of all Jenkins jobs. Accepts partial or informal folder names.

### get_failed_jobs

Retrieves a list of failed Jenkins jobs. Includes `FAILURE` and `UNSTABLE` by default;
pass `include_aborted=true` to also include `ABORTED`. Returns `as_of` timestamp
showing data freshness. Accepts partial or informal folder names.

### analyze_failures

Provides Jenkins failure analysis. Accepts partial or informal folder names.

### analyze_jenkins_failure

Provides detailed failure analysis for a specific Jenkins job. Accepts partial
or informal job names; returns candidate list if ambiguous.

### get_jobs_in_view

Retrieves jobs within a Jenkins view. Accepts partial, informal, or slightly
misspelled view names; returns candidate list if ambiguous.

### get_long_running_jobs

Retrieves builds running longer than a specified duration. Accepts partial or
informal folder names.

### get_jenkins_metrics

Retrieves Jenkins health and performance metrics. Accepts partial or informal
folder names.

### get_build_history

Retrieves historical builds for a job with date/days filtering. Accepts partial
or informal job names; returns candidate list if ambiguous.

### search_jobs

Finds Jenkins jobs by partial, informal, or misspelled names. Call this first
whenever the user doesn't give an exact full job path. Returns resolution status
and candidate jobs if ambiguous.

### list_views

Lists all Jenkins views. Call this first when the user doesn't know the exact
view name.

---

## Fuzzy Matching and Job Resolution

All tools that accept job, folder, or view names use a tiered fuzzy resolver:

1. **Exact match** — full path or short name (case-insensitive)
2. **Token match** — all query tokens appear in the path
3. **Fuzzy match** — `rapidfuzz` scoring for typos and partial matches

When a query is ambiguous, tools return a `candidates` list instead of an
error, allowing the AI to ask the user to clarify.

The resolver reads from an immutable `JobIndex` snapshot built by the
background warmer. This means:
- User requests never block on a live Jenkins walk
- All tools see consistent data
- Thread-safe reads without locking

---

```

## Clone the Repository

```
cd /opt

git clone  https://@bitbucket.org/lla-dev/jenkins-mcp-server.git jenkins-mcp-server

cd /opt/jenkins-mcp-server
```
# Create a .env file
# Update the .env file with the actual Jenkins URL,
# Jenkins username, and Jenkins API token.

---

## Setup

Create a Python virtual environment:

```
python -m venv venv
```

Activate the virtual environment:

```
source venv/bin/activate
```

Upgrade pip:

```
pip install --upgrade pip
```

Install dependencies:

```
pip install -r requirements.txt
```

---

## Run Locally

Stop any existing Uvicorn process:

```
pkill -f uvicorn
```

Start the application:

```
python -m uvicorn app.main:app --host 0.0.0.0 --port 8090
```

---

## Configure as a Service

Move the service file:

```
sudo mv /opt/jenkins-mcp-server/jenkins-mcp.service /etc/systemd/system/jenkins-mcp.service
```

Verify:

```
ls -l /etc/systemd/system/jenkins-mcp.service
```

---

## Service File

```ini
[Unit]
Description=Jenkins MCP Server
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/jenkins-mcp-server
ExecStart=/opt/jenkins-mcp-server/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Enable and Start Service

Reload systemd:

```
sudo systemctl daemon-reload
```

Enable auto-start:

```
sudo systemctl enable jenkins-mcp
```

Start service:

```
sudo systemctl start jenkins-mcp
```

Check status:

```
sudo systemctl status jenkins-mcp
```

View logs:

```
sudo journalctl -u jenkins-mcp -f
```

---

## Verify Auto Startup

Reboot the server:

```
sudo reboot
```

After the server comes back online:

```
sudo systemctl status jenkins-mcp
```

Expected:

```text
active (running)
```

---

## Update Existing Deployment

Navigate to the project directory:

```
cd /opt/jenkins-mcp-server
```

Pull the latest changes:

```
git pull origin feature/server_updates
```

Activate the virtual environment:

```
source venv/bin/activate
```

Install if any new dependencies:

```
pip install -r requirements.txt //only if there are new requiremnets
```

Restart the service:

```
sudo systemctl restart jenkins-mcp
```

Check status:

```
sudo systemctl status jenkins-mcp
```

---

## Important Notes

- Jenkins credentials are loaded from the `.env` file.
- Do not commit real credentials to Bitbucket.
- Store only dummy values in `.env.example`.
- The MCP Server runs on port **8090**.
- The service automatically restarts if the application crashes.
- The service automatically starts whenever the EC2 instance boots.
- If the EC2 instance is stopped, the MCP Server will also stop.
- When the EC2 instance starts again, the MCP Server automatically starts because the service is enabled through systemd.

---

## Endpoints

### Register Client

```http
POST /register
```

### Generate OAuth Token

```http
POST /token
```

### MCP Endpoint

```http
POST /mcp
```

Requires:

```text
Authorization: Bearer <access_token>
```