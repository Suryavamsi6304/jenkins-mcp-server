import app.jenkins_client as jenkins_client
from app.jenkins_client import (
    get_all_jobs_recursive,
    get_latest_build,
    get_build_log,
    find_job_by_name,
    get_job_details,
    get_builds_in_range,
    get_build_details,
    get_jobs_in_view as fetch_jobs_in_view,
    get_all_views,
)
from app.analyzer import analyze_log
from app.config import JENKINS_FETCH_TIMEOUT_SECONDS, JENKINS_MAX_WORKERS, JOBS_CACHE_TTL_SECONDS, MAX_BUILDS_PER_JOB
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
import time

_metrics_cache = {}
_metrics_cache_expires_at = 0


def _map_concurrently(fn, items, deadline=None):
    """
    Run fn(item) across items in parallel; these are independent Jenkins API calls.

    When `deadline` (a time.monotonic() timestamp) is supplied and reached before
    every item finishes, stop waiting and return whatever completed so far.
    Returns (results, truncated); `results` holds None for any item that did not
    complete in time, in its original position.
    """
    if not items:
        return [], False

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=JENKINS_MAX_WORKERS) as pool:
        futures = {pool.submit(fn, item): index for index, item in enumerate(items)}

        if deadline is None:
            for future, index in futures.items():
                results[index] = future.result()
            return results, False

        pending = set(futures)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining)
            for future in done:
                results[futures[future]] = future.result()

        for future in pending:
            future.cancel()

        return results, bool(pending)


# ---------------- FIX DETAILS ----------------
def _build_fix_details(reason):
    fixes = {
        "Pipeline syntax error → Fix Jenkinsfile": "Fix Jenkinsfile syntax.",
        "Compilation error → Fix pipeline": "Fix WorkflowScript.",
        "Memory issue": "Increase heap.",
        "Dependency issue": "Check dependencies.",
        "Permission issue": "Check credentials.",
        "Service issue": "Check connectivity.",
        "Timeout": "Increase timeout.",
        "Generic error": "Inspect logs.",
    }
    return fixes.get(reason, "Check logs.")


# ---------------- METRIC HELPERS ----------------
def _parse_metric_datetime(value, is_end=False):
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except:
        return None


def _build_metric_filters(project=None, application=None, environment=None):
    return [x.strip().lower() for x in [project, application, environment] if x]


def _job_matches_filters(job_name, filters):
    if not filters:
        return True
    return all(f in job_name.lower() for f in filters)


# ---------------- FIXED METRICS ----------------
def get_jenkins_metrics(
    metric,
    project=None,
    application=None,
    environment=None,
    start_date=None,
    end_date=None,
    mode="count",
    day="day",
    folder_name=None,
):
    global _metrics_cache
    global _metrics_cache_expires_at

    metric_name = (metric or "").strip().lower()
    mode_name = (mode or "count").strip().lower()

    allowed_metrics = {
        "jobs_triggered",
        "success_count",
        "failure_count",
        "success_rate",
        "failure_rate",
        "old_branches",
        "unused_jobs",
        "deployments_completed",
    }

    if metric_name not in allowed_metrics:
        return {
            "error": "Invalid metric requested",
            "allowed_metrics": sorted(allowed_metrics),
        }

    cache_key = (
        metric_name,
        project,
        application,
        environment,
        start_date,
        end_date,
        folder_name,
        mode_name,
    )
    cache_now = time.monotonic()
    if _metrics_cache_expires_at and cache_now >= _metrics_cache_expires_at:
        _metrics_cache.clear()
        _metrics_cache_expires_at = 0

    if cache_key in _metrics_cache and _metrics_cache_expires_at and cache_now < _metrics_cache_expires_at:
        return _metrics_cache[cache_key]

    start_dt = _parse_metric_datetime(start_date) if start_date else None
    end_dt = _parse_metric_datetime(end_date, is_end=True) if end_date else None
    filters = _build_metric_filters(project, application, environment)

    jobs = get_all_jobs_recursive(folder_name=folder_name)
    matching_jobs = 0
    total_builds = 0
    success_count = 0
    failure_count = 0
    unstable_count = 0
    aborted_count = 0
    other_count = 0
    old_jobs_over_1_year = 0
    unused_jobs = 0
    job_breakdown = []
    now = datetime.now(timezone.utc)
    one_year_ago = now.replace(year=now.year - 1)

    candidate_jobs = [job for job in jobs if _job_matches_filters(job.get("name") or "", filters)]

    # Jobs discovered with `builds` already embedded (from the skeleton query)
    # don't need a second per-job Jenkins request at all.
    jobs_with_builds = [job for job in candidate_jobs if isinstance(job.get("builds"), list)]
    jobs_needing_fetch = [job for job in candidate_jobs if not isinstance(job.get("builds"), list)]

    fetch_deadline = time.monotonic() + JENKINS_FETCH_TIMEOUT_SECONDS
    fetched_details, fetch_truncated = _map_concurrently(
        get_job_details, jobs_needing_fetch, deadline=fetch_deadline
    ) if jobs_needing_fetch else ([], False)

    job_data_pairs = [(job, {"url": job.get("url"), "builds": job.get("builds")}) for job in jobs_with_builds]
    job_data_pairs.extend(zip(jobs_needing_fetch, fetched_details))

    for job, job_data in job_data_pairs:
        job_name = job.get("name") or ""
        if not job_data:
            continue

        matching_jobs += 1
        job_url = job_data.get("url") or job.get("url")
        builds = job_data.get("builds") or []
        job_build_count = 0
        job_success = 0
        job_failure = 0
        job_unstable = 0
        job_aborted = 0
        job_other = 0
        latest_timestamp = None
        has_builds = bool(builds)

        for build in builds:
            build_data = get_build_details(build) or build
            build_timestamp = build_data.get("timestamp")
            build_dt = datetime.fromtimestamp(build_timestamp / 1000, tz=timezone.utc) if build_timestamp else None

            if build_dt and start_dt and build_dt < start_dt:
                continue
            if build_dt and end_dt and build_dt > end_dt:
                continue

            build_result = (build_data.get("result") or "UNKNOWN").upper()

            job_build_count += 1
            total_builds += 1
            latest_timestamp = max(latest_timestamp, build_timestamp) if latest_timestamp and build_timestamp else (build_timestamp or latest_timestamp)

            if build_result == "SUCCESS":
                success_count += 1
                job_success += 1
            elif build_result == "FAILURE":
                failure_count += 1
                job_failure += 1
            elif build_result == "UNSTABLE":
                unstable_count += 1
                job_unstable += 1
            elif build_result == "ABORTED":
                aborted_count += 1
                job_aborted += 1
            else:
                other_count += 1
                job_other += 1

        if not has_builds:
            unused_jobs += 1

        if latest_timestamp:
            latest_dt = datetime.fromtimestamp(latest_timestamp / 1000, tz=timezone.utc)
            if latest_dt < one_year_ago:
                old_jobs_over_1_year += 1

        job_breakdown.append({
            "job": job_name,
            "job_url": job_url,
            "builds_count": job_build_count,
            "success": job_success,
            "failure": job_failure,
            "unstable": job_unstable,
            "aborted": job_aborted,
            "other": job_other,
            "latest_timestamp": latest_timestamp,
        })

    compact_breakdown = [
        {
            "job": item["job"],
            "builds_count": item["builds_count"],
            "success": item["success"],
            "failure": item["failure"],
        }
        for item in job_breakdown
    ]

    metric_map = {
        "jobs_triggered": matching_jobs,
        "success_count": success_count,
        "failure_count": failure_count,
        "deployments_completed": success_count,
        "old_branches": old_jobs_over_1_year,
        "unused_jobs": unused_jobs,
        "success_rate": round((success_count / total_builds) * 100, 2) if total_builds else 0,
        "failure_rate": round((failure_count / total_builds) * 100, 2) if total_builds else 0,
    }

    value = metric_map[metric_name]
    unit = "%" if metric_name in {"success_rate", "failure_rate"} or mode_name == "percentage" else "count"

    result = {
        "metric": metric_name,
        "value": value,
        "unit": unit,
        "matching_jobs": matching_jobs,
        "total_builds": total_builds,
        "success_count": success_count,
        "failure_count": failure_count,
        "old_jobs_over_1_year": old_jobs_over_1_year,
        "unused_jobs": unused_jobs,
        "job_breakdown": compact_breakdown,
        "truncated": bool(getattr(jobs, "truncated", False)) or fetch_truncated,
    }
    _metrics_cache[cache_key] = result
    _metrics_cache_expires_at = cache_now + JOBS_CACHE_TTL_SECONDS
    return result


# ---------------- NEW: BUILD HISTORY ----------------
def get_build_history(
    job_name: str | None = None,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = MAX_BUILDS_PER_JOB,
    folder_name: str | None = None,
):
    """
    Return historical builds for a job. Supports either `days` or an explicit `start_date`/`end_date`.

    This function will page through builds using `get_builds_in_range` until the requested
    time window is satisfied or no more builds exist.
    """
    jobs_to_process = []

    if page_size < 1:
        return {"error": "page_size must be greater than zero", "builds": []}

    is_wildcard = not job_name or job_name == "*"
    if is_wildcard:
        jobs_to_process = get_all_jobs_recursive(folder_name=folder_name)
    else:
        job = find_job_by_name(job_name, folder_name=folder_name)

        if not job:
            return {
                "error": "Job not found",
                "builds": []
            }

        jobs_to_process = [job]

    # Convert date filters if provided; support `days` shorthand
    if days and not start_date:
        start_dt = datetime.now(timezone.utc) - timedelta(days=int(days))
        start_ts = start_dt
    else:
        start_ts = _parse_metric_datetime(start_date)

    end_ts = _parse_metric_datetime(end_date)

    builds = []
    seen_builds = set()
    fetch_deadline = time.monotonic() + JENKINS_FETCH_TIMEOUT_SECONDS if is_wildcard else None
    truncated = bool(getattr(jobs_to_process, "truncated", False)) if is_wildcard else False
    jobs_processed = 0

    for job in jobs_to_process:

        if fetch_deadline is not None and time.monotonic() >= fetch_deadline:
            truncated = True
            break

        jobs_processed += 1
        start_index = 0
        build_pages_cache = {}
        job_build_count = 0

        while True:

            page = get_builds_in_range(
                job.get("url"),
                start_index,
                page_size,
                cache=build_pages_cache,
            )

            if not page:
                break

            page_builds = page.get("builds") or []

            if not page_builds:
                break

            stop_processing_job = False

            for b in page_builds:

                build_number = b.get("number")
                build_key = (job.get("name"), build_number)

                if build_key in seen_builds:
                    continue

                seen_builds.add(build_key)

                b["job_name"] = job.get("name")

                ts = b.get("timestamp")

                try:
                    b_dt = (
                        datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                        if ts
                        else None
                    )
                except Exception:
                    continue

                if start_ts and b_dt and b_dt < start_ts:
                    stop_processing_job = True
                    break

                if end_ts and b_dt and b_dt > end_ts:
                    continue

                builds.append(b)
                job_build_count += 1
                if job_build_count >= page_size:
                    stop_processing_job = True
                    break

            if stop_processing_job:
                break

            if len(page_builds) < page_size:
                break

            start_index += page_size

    builds.sort(key=lambda b: b.get("timestamp") or 0, reverse=True)

    return {
        "job": job_name or "ALL_JOBS",
        "jobs_discovered": len(jobs_to_process),
        "jobs_processed": jobs_processed,
        "build_count": len(builds),
        "builds": builds,
        "truncated": truncated,
    }


# ---------------- ✅ FIXED ALL JOB STATUS ----------------
def get_all_jobs_status(folder_name=None):
    jobs = get_all_jobs_recursive(folder_name=folder_name)

    # Jobs discovered via the skeleton query already carry their latest build;
    # only jobs missing that (e.g. branches Jenkins didn't return lastBuild for)
    # need a live per-job Jenkins request.
    jobs_with_status = [job for job in jobs if job.get("status_known")]
    jobs_needing_fetch = [job for job in jobs if not job.get("status_known")]

    fetch_deadline = time.monotonic() + JENKINS_FETCH_TIMEOUT_SECONDS
    fetched, fetch_truncated = _map_concurrently(
        get_latest_build, jobs_needing_fetch, deadline=fetch_deadline
    ) if jobs_needing_fetch else ([], False)

    results = []
    for job in jobs_with_status:
        if job.get("build_number") is None:
            continue
        results.append({
            "job": job.get("name"),
            "job_url": job.get("url"),
            "build_number": job.get("build_number"),
            "status": job.get("status"),
            "duration": job.get("duration"),
            "timestamp": job.get("timestamp"),
        })

    for latest in fetched:
        if not latest:
            continue
        results.append({
            "job": latest["job"],
            "job_url": latest["job_url"],
            "build_number": latest["build_number"],
            "status": latest["status"],
            "duration": latest["duration"],
            "timestamp": latest["timestamp"]
        })

    return {
        "total_builds": len(results),
        "jobs": results,
        "jobs_discovered": len(jobs),
        "truncated": bool(getattr(jobs, "truncated", False)) or fetch_truncated,
    }


# ----------------  FIXED FAILED JOBS ----------------
def get_failed_jobs(folder_name=None):
    if folder_name:
        data = get_all_jobs_status(folder_name=folder_name)
        failed = [
            j for j in data["jobs"]
            if (j["status"] or "").upper() == "FAILURE"
        ]

        return {
            "failed_count": len(failed),
            "failed_jobs": failed,
            "truncated": data.get("truncated", False),
        }

    if not jenkins_client._failed_jobs_cache:
        jenkins_client.get_all_jobs_recursive()

    failed = list(jenkins_client._failed_jobs_cache)
    return {
        "failed_count": len(failed),
        "failed_jobs": failed,
        "truncated": False,
    }


# ---------------- VIEW JOBS ----------------
def get_jobs_in_view(view_name=None):
    view_identifier = view_name
    if not view_identifier:
        return get_all_views()

    result = fetch_jobs_in_view(view_identifier)
    if isinstance(result, dict) and "jobs" in result:
        return result

    return {
        "view": view_identifier,
        "job_count": len(result),
        "jobs": result,
    }


# ----------------  FIXED LONG RUNNING ----------------
def get_long_running_jobs(min_duration_minutes=2, folder_name=None):
    threshold_ms = float(min_duration_minutes) * 60 * 1000
    jobs = get_all_jobs_recursive(folder_name=folder_name)

    jobs_with_builds = [job for job in jobs if isinstance(job.get("builds"), list)]
    jobs_needing_fetch = [job for job in jobs if not isinstance(job.get("builds"), list)]

    fetch_deadline = time.monotonic() + JENKINS_FETCH_TIMEOUT_SECONDS
    fetched_details, fetch_truncated = _map_concurrently(
        get_job_details, jobs_needing_fetch, deadline=fetch_deadline
    ) if jobs_needing_fetch else ([], False)

    job_data_pairs = [(job, {"fullName": job.get("name"), "builds": job.get("builds")}) for job in jobs_with_builds]
    job_data_pairs.extend(zip(jobs_needing_fetch, fetched_details))

    long_running = []

    for job, job_data in job_data_pairs:
        if not job_data:
            continue

        latest_long_build = None
        for build in job_data.get("builds", []):
            duration = build.get("duration") or 0
            if duration < threshold_ms:
                continue

            if latest_long_build is None:
                latest_long_build = build
                continue

            current_ts = build.get("timestamp") or 0
            latest_ts = latest_long_build.get("timestamp") or 0
            if current_ts > latest_ts:
                latest_long_build = build
            elif current_ts == latest_ts:
                current_number = build.get("number") or 0
                latest_number = latest_long_build.get("number") or 0
                if current_number > latest_number:
                    latest_long_build = build

        if latest_long_build:
            duration = latest_long_build.get("duration") or 0
            long_running.append({
                "job": job_data.get("fullName") or job.get("name"),
                "build_number": latest_long_build.get("number"),
                "status": latest_long_build.get("result"),
                "duration_minutes": round(duration / 60000, 2),
            })

    return {
        "job_count": len(long_running),
        "jobs": long_running,
        "truncated": bool(getattr(jobs, "truncated", False)) or fetch_truncated,
    }


# ---------------- ANALYSIS ----------------
def analyze_failures(folder_name=None):
    failed_data = get_failed_jobs(folder_name=folder_name)
    analysis = []

    for job in failed_data["failed_jobs"]:
        log = get_build_log(job["job_url"], job["build_number"])
        reason = analyze_log(log)

        analysis.append({
            "job": job["job"],
            "build": job["build_number"],
            "reason": reason
        })

    return {
        "total_failures": len(analysis),
        "analysis": analysis,
        "truncated": failed_data.get("truncated", False),
    }


# ---------------- SINGLE FAILURE ----------------
def analyze_jenkins_failure(job_name=None, build_number=None, folder_name=None):
    job_url = None
    if job_name:
        job = find_job_by_name(job_name, folder_name=folder_name)
        if job:
            job_url = job.get("url")
        else:
            return {"error": "Job not found"}

    target = job_url or job_name
    if not target:
        return {"error": "Missing job identifier"}

    if not build_number:
        latest = get_latest_build(target)
        if not latest or not latest.get("build_number"):
            return {"error": "No latest build found"}
        build_number = latest["build_number"]
        job_url = latest.get("job_url")
        build_url = latest.get("job_url") and urljoin(latest.get("job_url").rstrip("/" ) + "/", f"{build_number}/")
    else:
        build_url = job_url and urljoin(job_url.rstrip("/") + "/", f"{build_number}/")

    log = get_build_log(job_url, build_number)
    reason = analyze_log(log)

    if isinstance(reason, dict):
        reason_text = reason.get("reason", str(reason))
        error_text = reason.get("error", str(reason))
    else:
        reason_text = str(reason)
        error_text = str(reason)

    return {
        "job": job_name or job_url,
        "job_url": job_url,
        "build": build_number,
        "build_url": build_url,
        "console_url": build_url and build_url.rstrip("/") + "/console",
        "error": error_text,
        "reason": reason_text,
        "fix_details": _build_fix_details(reason_text),
        "past_incidents": []
    }
