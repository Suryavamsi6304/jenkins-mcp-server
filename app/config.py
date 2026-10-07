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
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
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

# ---------------- BACKGROUND JOB INDEX ----------------
# The index holds structure (job/folder/view paths) plus each job's lastBuild.
# It deliberately excludes build arrays: those force Jenkins to read one
# build.xml per build off disk, which is what makes a full walk slow.
JOBS_INDEX_REFRESH_SECONDS = int(os.getenv("JOBS_INDEX_REFRESH_SECONDS", "120"))
# Completeness beats latency here: nobody waits on the background walk, so give
# it room to finish the whole tree. An exceeded budget does not publish partial
# data, it just means this cycle is discarded and the next one retries.
JOBS_INDEX_TIMEOUT_SECONDS = int(os.getenv("JOBS_INDEX_TIMEOUT_SECONDS", "600"))
# Age at which a served snapshot is reported as stale to the caller.
JOBS_INDEX_STALE_AFTER_SECONDS = int(os.getenv("JOBS_INDEX_STALE_AFTER_SECONDS", "600"))
# Last-resort escape hatch. A Jenkins with one permanently failing folder would
# otherwise never produce a complete walk, leaving the index empty forever. Past
# this age an incomplete snapshot is published, loudly labelled partial.
JOBS_INDEX_MAX_AGE_SECONDS = int(os.getenv("JOBS_INDEX_MAX_AGE_SECONDS", "3600"))
# Shortest gap between two refresh attempts, so a hard Jenkins outage costs one
# walk per window instead of one per request.
JOBS_INDEX_RETRY_COOLDOWN_SECONDS = int(os.getenv("JOBS_INDEX_RETRY_COOLDOWN_SECONDS", "15"))
# Minimum similarity (0-100) for a fuzzy name match to be offered as a
# suggestion. Low enough to be helpful; never enough to act on by itself.
FUZZY_MIN_SCORE = int(os.getenv("FUZZY_MIN_SCORE", "60"))
# Similarity required to resolve a name without asking the user. Real typos
# score in the 90s ("Reportng" -> "Reporting" is 94), whereas a different word
# that merely shares letters sits in the 60s ("Marketing" -> "Reporting" is 66).
FUZZY_AUTO_RESOLVE_SCORE = int(os.getenv("FUZZY_AUTO_RESOLVE_SCORE", "85"))
# How far ahead of the runner-up the best fuzzy match must be to be acted on,
# so two near-identical names produce a question rather than a coin flip.
FUZZY_MARGIN = int(os.getenv("FUZZY_MARGIN", "8"))
# Optional response cap. 0 means no cap: return every matching job. Raise above
# zero only if the agent's context window is actually being overrun.
MAX_JOBS_PER_RESPONSE = int(os.getenv("MAX_JOBS_PER_RESPONSE", "0"))
