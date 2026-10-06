import os
from dotenv import load_dotenv

load_dotenv()

JENKINS_URL = os.getenv("JENKINS_URL")
USERNAME = os.getenv("JENKINS_USER")
API_TOKEN = os.getenv("JENKINS_TOKEN")
ALLOWED_OAUTH_REDIRECT_URIS = {
	redirect_uri.strip()
	for redirect_uri in os.getenv(
		"ALLOWED_OAUTH_REDIRECT_URIS",
		"https://id.atlassian.com/outboundAuth/finish",
	).split(",")
	if redirect_uri.strip()
}
REQUEST_TIMEOUT = 10
JOBS_CACHE_TTL_SECONDS = int(os.getenv("JOBS_CACHE_TTL_SECONDS", "300"))
MAX_BUILDS_PER_JOB = int(os.getenv("MAX_BUILDS_PER_JOB", "10"))
JENKINS_MAX_WORKERS = int(os.getenv("JENKINS_MAX_WORKERS", "8"))
MAX_CONSOLE_LOG_BYTES = max(1, int(os.getenv("MAX_CONSOLE_LOG_BYTES", "262144")))
# Hard budgets so Jenkins-wide tools return partial data instead of hanging
# until the MCP client's own timeout kills the request uncleanly.
JENKINS_DISCOVERY_TIMEOUT_SECONDS = int(os.getenv("JENKINS_DISCOVERY_TIMEOUT_SECONDS", "20"))
JENKINS_FETCH_TIMEOUT_SECONDS = int(os.getenv("JENKINS_FETCH_TIMEOUT_SECONDS", "25"))
# How many folder levels one Jenkins tree query resolves in a single request;
# a deeper tree with fewer requests is far faster than one request per folder.
JENKINS_SKELETON_MAX_DEPTH = int(os.getenv("JENKINS_SKELETON_MAX_DEPTH", "6"))
