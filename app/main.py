
import base64
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from pydantic import BaseModel

from mcp_server import mcp

from app import index
from app.tools import (
    get_all_jobs_status,
    get_failed_jobs,

    analyze_failures,
    analyze_jenkins_failure,
    get_jenkins_metrics,
    get_jobs_in_view,
    get_long_running_jobs,
)
from app.auth import (
    issue_access_token,
    parse_basic_authorization,
    register_dynamic_client,
    validate_access_token,
    validate_client_credentials,
    create_authorization_code,
    consume_authorization_code,
    get_client,
    validate_redirect_uri,
    validate_authorization_code,
)

logger = logging.getLogger(__name__)

PUBLIC_BASE_URL = "https://mcp.devops.lla.com"
MCP_RESOURCE_URL = f"{PUBLIC_BASE_URL}/mcp/mcp"
PROTECTED_RESOURCE_METADATA_URL = (
    f"{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp/mcp"
)

class JenkinsFailureRequest(BaseModel):
    job_name: str
    build_number: int | None = None
class JenkinsMetricsRequest(BaseModel):
    metric: str
    project: str | None = None
    application: str | None = None
    environment: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    mode: str | None = "count"
mcp_app = mcp.http_app()


@asynccontextmanager
async def lifespan(fastapi_app):
    # Warm the job index in the background so requests read a ready snapshot
    # instead of each paying for a live Jenkins walk.
    index.start_background_refresh()
    try:
        async with mcp_app.lifespan(fastapi_app):
            yield
    finally:
        index.stop_background_refresh()


app = FastAPI(
    title="Jenkins MCP Server",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/mcp", mcp_app)

@app.middleware("http")
async def require_mcp_bearer_token(request: Request, call_next):
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        auth_header = request.headers.get("Authorization", "")
        access_token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        if request.method != "OPTIONS" and not validate_access_token(access_token):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Missing, invalid, or expired bearer token"},
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="'
                        f"{PROTECTED_RESOURCE_METADATA_URL}"
                        '"'
                    )
                },
            )
    return await call_next(request)

class BearerToken:
    def __init__(self, request: Request):
        self.token = None
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            self.token = auth_header[7:]
async def require_bearer_token(token: BearerToken = Depends(BearerToken)) -> str:
    if not token.token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization bearer token")
    if not validate_access_token(token.token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token")
    return token.token
@app.get("/")
def health():
    # Counts only, no job names: enough to see whether the index is warm and how
    # old it is while testing, without exposing the Jenkins tree unauthenticated.
    snapshot = index.current()
    return {
        "status": "MCP Server Running",
        "index": snapshot.describe() if snapshot else {"ready": False},
    }
@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    return {
        "issuer": PUBLIC_BASE_URL,
        "authorization_endpoint": f"{PUBLIC_BASE_URL}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_BASE_URL}/oauth/token",
        "registration_endpoint": f"{PUBLIC_BASE_URL}/oauth/register",
        "grant_types_supported": ["authorization_code"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_basic",
            "client_secret_post",
        ],
    }
@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp/mcp")
def protected_resource_metadata():
    return {
        "resource": MCP_RESOURCE_URL,
        "authorization_servers": [
            PUBLIC_BASE_URL
        ],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["read"],
    }
@app.post("/oauth/register")
async def register_client(payload: dict):
    name = payload.get("client_name") or payload.get("application_name") or "rovo-agent"
    redirect_uris = payload.get("redirect_uris") if isinstance(payload.get("redirect_uris"), list) else []
    token_endpoint_auth_method = payload.get("token_endpoint_auth_method", "none")
    try:
        client = register_dynamic_client(
            client_name=name,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    logger.info(
        "OAuth client registered token_auth_method=%s",
        client["token_endpoint_auth_method"],
    )
    response = {
        "client_id": client["client_id"],
        "grant_types": client["grant_types"],
        "scope": client["scope"],
        "token_endpoint_auth_method": client["token_endpoint_auth_method"],
    }
    if client["client_secret"]:
        response["client_secret"] = client["client_secret"]
    return JSONResponse(response)
@app.get("/oauth/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = ""
):
    if not validate_redirect_uri(client_id, redirect_uri):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid client_id or redirect_uri")

    if code_challenge_method != "S256" or not code_challenge:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="S256 PKCE is required")

    code = create_authorization_code(
        client_id,
        redirect_uri,
        code_challenge,
        code_challenge_method,
    )
    return RedirectResponse(
        f"{redirect_uri}?code={code}&state={state}",
        status_code=302
    )
@app.post("/oauth/token")
async def token(request: Request):
    auth_header = request.headers.get("Authorization")
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type != "authorization_code":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported grant_type")
    # Try to get client credentials from Basic Auth header OR form body
    client_id = None
    client_secret = None
    if auth_header:
        # Try Basic Auth first
        credentials = parse_basic_authorization(auth_header)
        if credentials:
            client_id = credentials["client_id"]
            client_secret = credentials["client_secret"]
    # If no Basic Auth, check form body for client_id and client_secret
    if not client_id:
        client_id = form.get("client_id")
        client_secret = form.get("client_secret")
    if not client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing client_id")

    client = get_client(client_id)
    if not client:
        logger.info("OAuth token authentication rejected client_found=false")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client credentials")

    token_endpoint_auth_method = client["token_endpoint_auth_method"]
    if token_endpoint_auth_method == "none":
        if client_secret:
            logger.info("OAuth token authentication rejected token_auth_method=none client_credentials_present=true")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client credentials")
    elif not client_secret or not validate_client_credentials(client_id, client_secret):
        logger.info(
            "OAuth token authentication rejected token_auth_method=%s client_credentials_present=%s",
            token_endpoint_auth_method,
            bool(client_secret),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client credentials")

    logger.info("OAuth token authentication accepted token_auth_method=%s", token_endpoint_auth_method)
    code = form.get("code")
    redirect_uri = form.get("redirect_uri")
    if not code or not redirect_uri:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or redirect_uri")

    code_data = validate_authorization_code(code)
    if not code_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid authorization code")

    if not validate_redirect_uri(client_id, redirect_uri):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid redirect_uri")

    code_verifier = form.get("code_verifier", "")
    if not code_verifier:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code_verifier required for PKCE")

    import hashlib

    challenge_digest = hashlib.sha256(code_verifier.encode()).digest()
    computed_challenge = base64.urlsafe_b64encode(challenge_digest).decode().rstrip("=")
    if not hmac.compare_digest(computed_challenge, code_data["code_challenge"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code_verifier")

    if not consume_authorization_code(code, client_id, redirect_uri):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid authorization code")
    token_data = issue_access_token(client_id)
    if not token_data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to issue access token")
    return JSONResponse(token_data)
@app.post("/")
async def root_post(request: Request):
    return {
        "message": "POST root reached"
    }
# /mcp handlers removed — FastMCP is mounted at /mcp via app.mount
@app.get("/get_all_jobs_status", dependencies=[Depends(require_bearer_token)])
def api_all_jobs(folder_name: str | None = None, limit: int | None = None):
    return get_all_jobs_status(folder_name=folder_name, limit=limit)
@app.get("/get_jobs_in_view", dependencies=[Depends(require_bearer_token)])
def api_get_jobs_in_view(view_name: str | None = None):
    return get_jobs_in_view(view_name=view_name)
@app.get("/get_long_running_jobs", dependencies=[Depends(require_bearer_token)])
def api_get_long_running_jobs(min_duration_minutes: int = 2):
    return get_long_running_jobs(
        min_duration_minutes=min_duration_minutes,
    )
@app.get("/get_failed_jobs", dependencies=[Depends(require_bearer_token)])
def api_failed_jobs():
    return get_failed_jobs()
@app.get("/analyze_failures", dependencies=[Depends(require_bearer_token)])
def api_analyze_failures():
    return analyze_failures()
@app.post("/analyze_jenkins_failure", dependencies=[Depends(require_bearer_token)])
def api_analyze_jenkins_failure(request: JenkinsFailureRequest):
    return analyze_jenkins_failure(
        job_name=request.job_name,
        build_number=request.build_number,
    )
@app.get("/analyze_jenkins_failure", dependencies=[Depends(require_bearer_token)])
def api_analyze_jenkins_failure_get(job_name: str, build_number: int | None = None):
    return analyze_jenkins_failure(
        job_name=job_name,
        build_number=build_number,
    )
@app.post("/get_jenkins_metrics", dependencies=[Depends(require_bearer_token)])
def api_get_jenkins_metrics(request: JenkinsMetricsRequest):
    return get_jenkins_metrics(
        metric=request.metric,
        project=request.project,
        application=request.application,
        environment=request.environment,
        start_date=request.start_date,
        end_date=request.end_date,
        mode=request.mode,
    )
@app.get("/get_jenkins_metrics", dependencies=[Depends(require_bearer_token)])
def api_get_jenkins_metrics_get(
    metric: str,
    project: str | None = None,
    application: str | None = None,
    environment: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    mode: str | None = "count",
):
    return get_jenkins_metrics(
        metric=metric,
        project=project,
        application=application,
        environment=environment,
        start_date=start_date,
        end_date=end_date,
        mode=mode,
    )
 