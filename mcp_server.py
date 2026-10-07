from fastmcp import FastMCP
from typing import Any
from app.config import MAX_BUILDS_PER_JOB

#  Import your actual functions
from app.tools import (
    get_all_jobs_status,
    get_failed_jobs as get_failed_jobs_status,
    analyze_failures as analyze_failed_jobs,
    analyze_jenkins_failure,
    get_jobs_in_view as get_view_jobs,
    get_long_running_jobs as get_long_jobs,
    get_jenkins_metrics as get_metrics,
    get_build_history as fetch_build_history,
)

mcp = FastMCP("Jenkins MCP Server")

#  Tool 1: All jobs
@mcp.tool()
def get_all_jobs(folder_name: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """Retrieve the latest build status for Jenkins jobs.

    Returns every job by default. Set `folder_name` to scope to one folder at
    any depth; partial, differently-cased and misspelled folder names are
    resolved automatically.

    Check the `status` field of the response before reading `jobs`:
      - "ok"                -> `jobs` holds the result; `as_of` says how fresh it is
      - "not_found"         -> no folder matched; offer `did_you_mean` to the user
      - "ambiguous"         -> several folders matched; ask the user to pick from `candidates`
      - "index_unavailable" -> Jenkins could not be reached. This does NOT mean
                               there are no jobs; report it as a failure to look.

    An empty `jobs` list with status "ok" is a genuine zero. Set `limit` only to
    page a large result; omitting it returns everything.
    """
    return get_all_jobs_status(folder_name=folder_name, limit=limit)

# Tool 2: Failed jobs
@mcp.tool()
def get_failed_jobs(folder_name: str | None = None) -> dict[str, Any]:
    """Retrieve failed Jenkins jobs, optionally scoped to a top-level folder."""
    return get_failed_jobs_status(folder_name=folder_name)

# Tool 3: Analyze failures
@mcp.tool()
def analyze_failures(folder_name: str | None = None) -> dict[str, Any]:
    """Analyze logs for currently failed jobs, optionally scoped to a top-level folder."""
    return analyze_failed_jobs(folder_name=folder_name)

#  Tool 4: Deep analysis
@mcp.tool()
def analyze_failure(job_name: str, folder_name: str | None = None) -> dict[str, Any]:
    """Analyze the latest build failure for one Jenkins job identified by its name."""
    return analyze_jenkins_failure(job_name, folder_name=folder_name)

# Tool 5: Jobs in view
@mcp.tool()
def get_jobs_in_view(view_name: str) -> dict[str, Any]:
    """Retrieve jobs and their latest build status from a named Jenkins view."""
    return get_view_jobs(view_name=view_name)

# Tool 6: Long running jobs
@mcp.tool()
def get_long_running_jobs(min_duration_minutes: int = 5, folder_name: str | None = None) -> dict[str, Any]:
    """Retrieve Jenkins builds whose duration meets or exceeds the supplied minute threshold."""
    return get_long_jobs(min_duration_minutes, folder_name=folder_name)

# Tool 7: Metrics
@mcp.tool()
def get_jenkins_metrics(metric: str, folder_name: str | None = None) -> dict[str, Any]:
    """Retrieve a Jenkins metric such as failure_count, success_rate, or jobs_triggered."""
    return get_metrics(metric, folder_name=folder_name)


# Tool 8: Build history
@mcp.tool()
def get_build_history(job_name: str, days: int | None = None, start_date: str | None = None, end_date: str | None = None, page_size: int = MAX_BUILDS_PER_JOB, folder_name: str | None = None) -> dict[str, Any]:
    """Retrieve up to page_size recent builds per job, optionally scoped to a top-level folder."""
    return fetch_build_history(
        job_name=job_name,
        days=days,
        start_date=start_date,
        end_date=end_date,
        page_size=page_size,
        folder_name=folder_name,
    )

#  Start MCP server
if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8090)
 