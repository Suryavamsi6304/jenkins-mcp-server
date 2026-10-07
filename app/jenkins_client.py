import time
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib.parse import quote, urljoin, urlparse
from urllib3.util.retry import Retry
from app.config import (
    API_TOKEN,
    JENKINS_DISCOVERY_TIMEOUT_SECONDS,
    JENKINS_MAX_WORKERS,
    JENKINS_SKELETON_MAX_DEPTH,
    JENKINS_URL,
    JOBS_CACHE_TTL_SECONDS,
    MAX_BUILDS_PER_JOB,
    MAX_CONSOLE_LOG_BYTES,
    REQUEST_TIMEOUT,
    USERNAME,
)

logger = logging.getLogger(__name__)

# Reuse a single session so TCP/TLS connections are pooled instead of
# re-negotiated on every request; this was the biggest source of latency.
_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20,
    max_retries=Retry(total=2, backoff_factor=0.2, status_forcelist=[502, 503, 504]),
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# Short-lived cache for the full job tree walk, which is the most expensive
# call (many sequential Jenkins API requests) and is re-run by almost every tool.
_jobs_cache = {"data": None, "expires_at": 0}
_folder_jobs_cache = {}
_job_lookup_cache = {}
_failed_jobs_cache = []


def _jenkins_origin():
    if not JENKINS_URL:
        raise ValueError("JENKINS_URL must be configured")

    parsed_url = urlparse(JENKINS_URL)
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise ValueError("JENKINS_URL must use HTTPS and include a hostname")

    return parsed_url.scheme, parsed_url.hostname.lower(), parsed_url.port or 443


def _is_trusted_jenkins_url(url):
    try:
        scheme, hostname, port = _jenkins_origin()
        parsed_url = urlparse(url)
        return (
            parsed_url.scheme == scheme
            and parsed_url.hostname is not None
            and parsed_url.hostname.lower() == hostname
            and (parsed_url.port or 443) == port
            and not parsed_url.username
            and not parsed_url.password
        )
    except ValueError:
        return False


def _safe_get(url):
    if not _is_trusted_jenkins_url(url):
        logger.error("Jenkins request rejected because the URL does not match JENKINS_URL")
        return None

    try:
        if not USERNAME or not API_TOKEN:
            raise ValueError("Jenkins credentials must be configured")

        clean_username = USERNAME.strip()
        clean_token = API_TOKEN.strip()

        response = _session.get(
            url,
            auth=HTTPBasicAuth(clean_username, clean_token),
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
            verify=True,
            allow_redirects=False,
        )

        logger.info("Jenkins API request completed status=%s", response.status_code)

        if response.status_code != 200:
            logger.error("Jenkins API request failed status=%s", response.status_code)
            return None

        return response.json()

    except requests.RequestException as error:
        logger.error("Jenkins API request failed error_type=%s", type(error).__name__)
        return None
    except ValueError as error:
        logger.error("Jenkins API request failed error_type=%s", type(error).__name__)
        return None


def _job_api_url(job_or_url, suffix="api/json"):
    base_url = job_or_url.get("url") if isinstance(job_or_url, dict) else job_or_url

    if not base_url:
        return None

    if not _is_trusted_jenkins_url(base_url):
        return None

    if base_url.endswith("/api/json"):
        base_url = base_url.replace("/api/json", "")

    return urljoin(base_url.rstrip("/") + "/", suffix)


def _view_api_url(view_or_url):
    if not view_or_url:
        return None

    base_url = (
        view_or_url.get("url") if isinstance(view_or_url, dict)
        else view_or_url
    )

    if not base_url:
        return None

    if _is_trusted_jenkins_url(base_url):
        return (
            base_url
            if base_url.endswith("/api/json")
            else f"{base_url.rstrip('/')}/api/json"
        )

    return f"{JENKINS_URL}/view/{quote(base_url, safe='')}/api/json"


def find_job_by_name(job_name, folder_name=None):
    if not job_name:
        return None

    if not folder_name:
        if not _job_lookup_cache:
            get_all_jobs_recursive()

        cached_job = _job_lookup_cache.get(job_name)
        if cached_job is not None:
            return cached_job

        for name, job in _job_lookup_cache.items():
            if name.endswith(f"/{job_name}"):
                return job
        return None

    for job in get_all_jobs_recursive(folder_name=folder_name):
        if job.get("name") == job_name or job.get("name", "").endswith(f"/{job_name}"):
            return job
    return None


def find_view_by_name(view_name):
    if not view_name:
        return None

    target_name = str(view_name).strip()
    if not target_name:
        return None

    target_name_lc = target_name.lower()
    root_data = _safe_get(f"{JENKINS_URL}/api/json?tree=jobs[name,url,_class],views[name,url]")
    if not root_data:
        return None

    for view in root_data.get("views") or []:
        current_name = (view.get("name") or "").strip()
        if current_name.lower() == target_name_lc:
            view_url = view.get("url")
            if view_url:
                return view_url
            return f"{JENKINS_URL}/view/{quote(current_name, safe='')}/"

    for job in root_data.get("jobs") or []:
        if not isinstance(job, dict):
            continue

        job_class = (job.get("_class") or "").lower()
        is_folder = any(marker in job_class for marker in ("folder", "multibranch", "organizationfolder"))
        if not is_folder:
            continue

        folder_url = _job_api_url(job)
        if not folder_url:
            continue

        folder_base_url = folder_url[:-len("/api/json")] if folder_url.endswith("/api/json") else folder_url.rstrip("/")
        folder_data = _safe_get(f"{folder_base_url}/api/json?tree=views[name,url]")
        if not folder_data:
            continue

        for view in folder_data.get("views") or []:
            current_name = (view.get("name") or "").strip()
            if current_name.lower() == target_name_lc:
                view_url = view.get("url")
                if view_url:
                    return view_url
                return f"{folder_base_url}/view/{quote(current_name, safe='')}/"

    return None


def get_all_views():
    root_data = _safe_get(f"{JENKINS_URL}/api/json?tree=views[name,url]")

    if not root_data:
        return {
            "view_count": 0,
            "views": []
        }

    views = [
        {
            "name": view.get("name"),
            "url": view.get("url")
        }
        for view in root_data.get("views", [])
    ]

    return {
        "view_count": len(views),
        "views": views
    }


def get_job_details(job_or_url):
    """
    Request a large builds range so we fetch many builds for the job.
    """
    api_url = _job_api_url(job_or_url)
    if not api_url:
        return None

    tree = (
        "fullName,name,url,"
        f"builds[number,url,result,duration,timestamp]{{0,{MAX_BUILDS_PER_JOB}}},"
        "lastBuild[number,url,result,duration,timestamp]"
    )

    url = api_url
    if url.endswith("/api/json"):
        url = f"{url}?tree={tree}"
    else:
        url = urljoin(url.rstrip("/") + "/", f"api/json?tree={tree}")

    return _safe_get(url)


def get_latest_build(job_or_url):
    api_url = _job_api_url(job_or_url)
    if not api_url:
        return None

    tree = (
        "fullName,name,url,"
        "lastBuild[number,url,result,duration,timestamp],"
        "lastCompletedBuild[number,url,result,duration,timestamp]"
    )
    job_data = _safe_get(f"{api_url}?tree={tree}")
    if not job_data:
        return None

    last = job_data.get("lastBuild") or job_data.get("lastCompletedBuild")
    if not last:
        return None

    return {
        "job": job_data.get("fullName") or job_data.get("name"),
        "job_url": job_data.get("url"),
        "build_number": last.get("number"),
        "status": last.get("result"),
        "duration": last.get("duration"),
        "timestamp": last.get("timestamp"),
    }


def get_build_details(build_or_url):
    if isinstance(build_or_url, dict):
        return build_or_url
    return None


def get_latest_failed_build(job_or_url):
    job_data = get_job_details(job_or_url)
    if not job_data:
        return None

    for build in job_data.get("builds", []):
        result = build.get("result")
        if result == "FAILURE":
            build_url = build.get("url")
            return {
                "job": job_data.get("fullName") or job_data.get("name"),
                "job_url": job_data.get("url"),
                "build_number": build.get("number"),
                "build_url": build_url,
                "console_url": build_url and build_url.rstrip("/") + "/console",
                "status": result,
                "duration": build.get("duration"),
                "timestamp": build.get("timestamp"),
            }

    return None


class JobList(list):
    """
    A list of discovered jobs. `truncated` is True when the discovery time
    budget was reached before every folder could be visited, so callers know
    the result may be incomplete rather than treating it as the full tree.
    """
    truncated = False


def _skeleton_field_spec(remaining_depth, include_builds):
    """
    Build a Jenkins `tree` selector that asks for name/class/url/lastBuild
    (and recent builds) for several folder levels in one request, mirroring
    a single deep `tree` query instead of one HTTP call per folder per job.
    """
    base = "name,_class,url,lastBuild[number,result,duration,timestamp],branches[name,url,lastBuild[number,result,duration,timestamp]]"
    if include_builds:
        base += f",builds[number,result,duration,timestamp]{{0,{MAX_BUILDS_PER_JOB}}}"
    if remaining_depth <= 0:
        return base
    nested = _skeleton_field_spec(remaining_depth - 1, include_builds)
    return f"{base},jobs[{nested}]"


def _fetch_skeleton(url, max_depth, include_builds=True):
    tree = _skeleton_field_spec(max_depth, include_builds)
    separator = "&" if "?" in url else "?"
    return _safe_get(f"{url}{separator}tree={tree}")


def _parse_entry(entry, full_name, remaining_depth, results, unresolved):
    for branch in entry.get("branches") or []:
        b_name = branch.get("name")
        b_url = branch.get("url")
        if not (b_name and b_url):
            continue
        last_build = branch.get("lastBuild") or {}
        results.append({
            "name": f"{full_name}/{b_name}",
            "url": b_url,
            "api_url": _job_api_url(b_url),
            "status": last_build.get("result"),
            "build_number": last_build.get("number"),
            "duration": last_build.get("duration"),
            "timestamp": last_build.get("timestamp"),
            "status_known": "lastBuild" in branch,
        })

    entry_class = (entry.get("_class") or "").lower()
    is_container_class = any(
        marker in entry_class
        for marker in ("folder", "multibranch", "organizationfolder")
    )
    has_children_key = isinstance(entry.get("jobs"), list)

    if has_children_key:
        _parse_container_children(entry, full_name, results, unresolved, remaining_depth)
        return

    if is_container_class:
        if remaining_depth <= 0:
            # The query didn't go deep enough to see this folder's children.
            api_url = _job_api_url(entry)
            if api_url:
                unresolved.append((api_url, full_name))
        # Otherwise Jenkins reported no children within the requested depth,
        # i.e. a genuinely empty folder; it contributes no jobs.
        return

    last_build = entry.get("lastBuild") or {}
    job = {
        "name": full_name,
        "url": entry.get("url"),
        "api_url": _job_api_url(entry),
        "status": last_build.get("result"),
        "build_number": last_build.get("number"),
        "duration": last_build.get("duration"),
        "timestamp": last_build.get("timestamp"),
        "status_known": True,
    }
    if isinstance(entry.get("builds"), list):
        job["builds"] = entry.get("builds")
    results.append(job)


def _parse_container_children(node, full_name, results, unresolved, remaining_depth):
    for entry in node.get("jobs") or []:
        name = entry.get("name")
        if not name:
            continue
        child_full_name = f"{full_name}/{name}" if full_name else name
        _parse_entry(entry, child_full_name, remaining_depth - 1, results, unresolved)


def get_all_jobs_recursive(folder_name=None):
    """
    Discover jobs (and their latest build / recent builds) using a small
    number of deep Jenkins `tree` queries instead of one HTTP request per
    folder plus a second request per job.

    Each query resolves up to JENKINS_SKELETON_MAX_DEPTH folder levels in one
    round trip; any folder still unresolved at that depth is queued for a
    follow-up query, run concurrently (bounded by JENKINS_MAX_WORKERS). The
    whole walk stops once JENKINS_DISCOVERY_TIMEOUT_SECONDS elapses. Returns a
    JobList; `.truncated` is True when the budget was reached first, in which
    case the result is not cached so a later call can retry a full walk.
    """
    now = time.monotonic()
    if folder_name:
        folder_cache = _folder_jobs_cache.get(folder_name)
        if folder_cache and now < folder_cache["expires_at"]:
            return folder_cache["data"]
        if _jobs_cache["data"] is not None and now < _jobs_cache["expires_at"]:
            prefix = f"{folder_name}/"
            filtered = JobList(
                job for job in _jobs_cache["data"] if job.get("name", "").startswith(prefix)
            )
            return filtered
    elif _jobs_cache["data"] is not None and now < _jobs_cache["expires_at"]:
        return _jobs_cache["data"]

    if folder_name:
        start_url = f"{JENKINS_URL}/job/{quote(folder_name, safe='')}/api/json"
    else:
        start_url = f"{JENKINS_URL}/api/json"

    deadline = time.monotonic() + JENKINS_DISCOVERY_TIMEOUT_SECONDS
    results = JobList()
    truncated = False
    frontier = [(start_url, folder_name or "")]

    while frontier:
        if time.monotonic() >= deadline:
            truncated = True
            break

        batch = frontier
        frontier = []

        with ThreadPoolExecutor(max_workers=JENKINS_MAX_WORKERS) as pool:
            future_map = {
                pool.submit(_fetch_skeleton, url, JENKINS_SKELETON_MAX_DEPTH): (url, full_name)
                for url, full_name in batch
            }
            for future in as_completed(future_map):
                url, full_name = future_map[future]
                node = future.result()
                if not node:
                    continue
                unresolved = []
                _parse_container_children(node, full_name, results, unresolved, JENKINS_SKELETON_MAX_DEPTH)
                frontier.extend(unresolved)

    results.truncated = truncated

    if not truncated:
        expires_at = time.monotonic() + JOBS_CACHE_TTL_SECONDS
        if folder_name:
            _folder_jobs_cache[folder_name] = {"data": results, "expires_at": expires_at}
        else:
            _jobs_cache["data"] = results
            _jobs_cache["expires_at"] = expires_at

            _job_lookup_cache.clear()
            _failed_jobs_cache.clear()
            for job in results:
                name = job.get("name")
                if name:
                    _job_lookup_cache[name] = job
                if (job.get("status") or "").upper() == "FAILURE":
                    _failed_jobs_cache.append(job)

    return results


def get_jobs_in_view(view_or_url):
    resolved_view = view_or_url
    if isinstance(view_or_url, str):
        resolved_view = find_view_by_name(view_or_url) or view_or_url

    view_url = _view_api_url(resolved_view)
    if not view_url:
        return {"view": view_or_url, "job_count": 0, "jobs": []}

    query = "?tree=jobs[name,url,color,lastBuild[number,result,duration,timestamp],builds[number]]"
    data = _safe_get(view_url + query)
    if not data:
        return {"view": view_or_url, "job_count": 0, "jobs": []}

    raw_jobs = data.get("jobs", [])
    enriched = []

    for job in raw_jobs:
        name = job.get("name")
        job_url = job.get("url")

        builds = job.get("builds") or []
        build_count = len(builds) if isinstance(builds, list) else None

        last = job.get("lastBuild") or {}
        last_build_number = last.get("number")
        last_build_status = last.get("result")

        enriched.append({
            "name": name,
            "job_url": job_url,
            "build_count": build_count,
            "last_build_number": last_build_number,
            "last_build_status": last_build_status
        })

    return {
        "view": data.get("name") or view_or_url,
        "job_count": len(enriched),
        "jobs": enriched
    }


def get_build_log(job_or_url, build_number):
    try:
        base_url = job_or_url.get("url") if isinstance(job_or_url, dict) else job_or_url
        if not _is_trusted_jenkins_url(base_url):
            logger.error("Jenkins build log request rejected because the URL does not match JENKINS_URL")
            return ""

        if not USERNAME or not API_TOKEN:
            raise ValueError("Jenkins credentials must be configured")

        url = urljoin(base_url.rstrip("/") + "/", f"{build_number}/consoleText")

        response = _session.get(
            url,
            auth=HTTPBasicAuth(USERNAME.strip(), API_TOKEN.strip()),
            headers={"Range": f"bytes=-{MAX_CONSOLE_LOG_BYTES}"},
            timeout=REQUEST_TIMEOUT,
            verify=True,
            allow_redirects=False,
            stream=True,
        )

        if response.status_code not in (200, 206):
            logger.error("Jenkins build log request failed status=%s", response.status_code)
            return ""

        content_range = response.headers.get("Content-Range", "")
        range_start = 0
        if response.status_code == 206 and content_range.startswith("bytes "):
            try:
                range_start = int(content_range[6:].split("-", 1)[0])
            except (ValueError, IndexError):
                range_start = 0

        content_length = int(response.headers.get("Content-Length", "0") or 0)
        truncated = range_start > 0 or content_length > MAX_CONSOLE_LOG_BYTES
        chunks = []
        total_bytes = 0
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                remaining = MAX_CONSOLE_LOG_BYTES - total_bytes
                if remaining <= 0:
                    truncated = True
                    break
                chunks.append(chunk[:remaining])
                total_bytes += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    truncated = True
                    break
        finally:
            response.close()

        log_text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        if truncated:
            portion = "tail" if response.status_code == 206 and range_start > 0 else "beginning"
            return f"[Console log truncated; showing the {portion} up to {MAX_CONSOLE_LOG_BYTES} bytes.]\n{log_text}"
        return log_text

    except requests.RequestException as error:
        logger.error("Jenkins build log request failed error_type=%s", type(error).__name__)
        return ""
    except ValueError as error:
        logger.error("Jenkins build log request failed error_type=%s", type(error).__name__)
        return ""


def get_builds_in_range(job_or_url, start_index: int, page_size: int, cache=None):
    """
    Fetch a bounded Jenkins build page, with a cached full-list fallback for
    Jenkins versions or plugins that ignore tree range selectors.

    - `start_index`: zero-based start index
    - `page_size`: number of builds to return

    Returns a dict compatible with `get_job_details` but with `builds` containing
    only the requested slice.
    """
    api_url = _job_api_url(job_or_url)
    if not api_url or page_size < 1 or start_index < 0:
        return None

    cache_entry = cache.get(api_url) if cache is not None else None
    if cache_entry and cache_entry.get("mode") == "all":
        data = cache_entry["data"]
        paged_builds = data["allBuilds"][start_index:start_index + page_size]
    else:
        tree = (
            "fullName,name,url,"
            f"builds[number,id,url,result,duration,timestamp]{{{start_index},{page_size}}},"
            "lastBuild[number,url,result,duration,timestamp]"
        )
        ranged_data = _safe_get(f"{api_url}?tree={tree}")
        if ranged_data is None:
            return None
        paged_builds = ranged_data.get("builds")
        first_page_signature = cache_entry.get("first_page_signature") if cache_entry else None
        repeated_first_page = (
            start_index > 0
            and paged_builds
            and first_page_signature == _build_page_signature(paged_builds)
        )
        empty_first_page = start_index == 0 and not paged_builds and ranged_data.get("lastBuild")

        if (
            not isinstance(paged_builds, list)
            or len(paged_builds) > page_size
            or repeated_first_page
            or empty_first_page
        ):
            all_tree = (
                "fullName,name,url,"
                "allBuilds[number,id,url,result,duration,timestamp],"
                "lastBuild[number,url,result,duration,timestamp]"
            )
            data = _safe_get(f"{api_url}?tree={all_tree}")
            if not data:
                return None
            data = {
                **data,
                "allBuilds": sorted(
                    data.get("allBuilds") or [],
                    key=lambda build: build.get("timestamp") or 0,
                    reverse=True,
                ),
            }
            if cache is not None:
                cache[api_url] = {"mode": "all", "data": data}
            paged_builds = data["allBuilds"][start_index:start_index + page_size]
        else:
            data = ranged_data
            if cache is not None and start_index == 0:
                cache[api_url] = {
                    "mode": "range",
                    "first_page_signature": _build_page_signature(paged_builds),
                }

    return {
        "name": data.get("name"),
        "fullName": data.get("fullName"),
        "url": data.get("url"),
        "lastBuild": data.get("lastBuild"),
        "builds": paged_builds,
    }


def _build_page_signature(builds):
    return tuple(
        build.get("number", build.get("id", build.get("url", build.get("timestamp"))))
        for build in builds
    )
