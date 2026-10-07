"""
Background-refreshed snapshot of the Jenkins tree, plus the name resolver that
every tool should route user-supplied names through.

Two things make this different from the ad-hoc caches it replaces:

* The expensive, volatile part is not here. The snapshot carries structure
  (job / folder / view paths) plus each job's `lastBuild`. It deliberately does
  not carry build arrays, because asking Jenkins for `builds[...]` forces it to
  read one build.xml per build off disk -- that is what makes a full walk slow.
  Build history, console logs and metrics stay live and scoped to one job.

* A refresh that fails, times out, or comes back empty never replaces a good
  snapshot. Readers get the last known-good data with its age attached, and a
  caller can always tell "Jenkins is unreachable" apart from "there is nothing
  here". The old caches stored failures as fact for JOBS_CACHE_TTL_SECONDS.

Readers call `ensure()`, which returns a snapshot or None, and never blocks on
a live walk once the background thread is running.
"""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

import app.jenkins_client as jenkins_client
from app.config import (
    FUZZY_AUTO_RESOLVE_SCORE,
    FUZZY_MARGIN,
    FUZZY_MIN_SCORE,
    JENKINS_MAX_WORKERS,
    JENKINS_SKELETON_MAX_DEPTH,
    JOBS_INDEX_MAX_AGE_SECONDS,
    JOBS_INDEX_REFRESH_SECONDS,
    JOBS_INDEX_RETRY_COOLDOWN_SECONDS,
    JOBS_INDEX_STALE_AFTER_SECONDS,
    JOBS_INDEX_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

CONTAINER_MARKERS = ("folder", "multibranch", "organizationfolder")
MAX_CANDIDATES = 10


# ---------------- SNAPSHOT ----------------
@dataclass(frozen=True)
class JobEntry:
    name: str
    url: str
    status: str | None
    build_number: int | None
    duration: int | None
    timestamp: int | None


@dataclass(frozen=True)
class ViewEntry:
    name: str
    path: str
    url: str
    folder: str


@dataclass(frozen=True)
class Snapshot:
    """
    An immutable view of the Jenkins tree. Swapped in whole, so a reader can
    never observe one being rebuilt -- the previous caches cleared and
    repopulated in place, which let concurrent requests see a half-built index.
    """

    jobs: tuple[JobEntry, ...] = ()
    folders: tuple[str, ...] = ()
    views: tuple[ViewEntry, ...] = ()
    as_of: float = 0.0
    build_seconds: float = 0.0
    requests_made: int = 0
    complete: bool = False
    errors: tuple[str, ...] = ()
    _by_folder: dict[str, tuple[JobEntry, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.as_of)

    @property
    def stale(self) -> bool:
        return self.age_seconds > JOBS_INDEX_STALE_AFTER_SECONDS

    @property
    def as_of_iso(self) -> str:
        return datetime.fromtimestamp(self.as_of, tz=timezone.utc).isoformat()

    def jobs_under(self, folder: str | None) -> tuple[JobEntry, ...]:
        """
        Jobs inside `folder`, at any depth. `folder` must be a canonical path
        from this snapshot (what `resolve_folder` returns), so nested paths like
        "Payments/API" work -- the old folder scoping built a URL with
        quote(folder, safe="") and 404'd on anything containing a slash.
        """
        if not folder:
            return self.jobs
        return self._by_folder.get(folder, ())

    def describe(self) -> dict:
        return {
            "as_of": self.as_of_iso,
            "age_seconds": round(self.age_seconds, 1),
            "stale": self.stale,
            "complete": self.complete,
            "jobs_indexed": len(self.jobs),
            "folders_indexed": len(self.folders),
            "views_indexed": len(self.views),
            "build_seconds": round(self.build_seconds, 2),
            "requests_made": self.requests_made,
        }


def _finalize(jobs, folders, views, started_at, requests_made, errors) -> Snapshot:
    jobs = tuple(jobs)
    folders = tuple(sorted(set(folders)))

    by_folder: dict[str, list[JobEntry]] = {folder: [] for folder in folders}
    for job in jobs:
        segments = job.name.split("/")
        for depth in range(1, len(segments)):
            parent = "/".join(segments[:depth])
            bucket = by_folder.get(parent)
            if bucket is not None:
                bucket.append(job)

    return Snapshot(
        jobs=jobs,
        folders=folders,
        views=tuple(views),
        as_of=time.time(),
        build_seconds=time.monotonic() - started_at,
        requests_made=requests_made,
        complete=not errors,
        errors=tuple(errors),
        _by_folder={folder: tuple(items) for folder, items in by_folder.items()},
    )


# ---------------- JENKINS QUERY ----------------
def _level_spec(depth: int) -> str:
    """
    A `tree` selector covering `depth` folder levels in one request.

    Note what is absent: `builds[...]`. Jenkins serves names, `_class`, `url`,
    `views` and `lastBuild` from its in-memory item tree, so this stays cheap
    however many jobs exist.
    """
    base = (
        "name,_class,url,views[name,url],"
        "lastBuild[number,result,duration,timestamp],"
        "branches[name,url,lastBuild[number,result,duration,timestamp]]"
    )
    spec = base
    for _ in range(max(0, depth)):
        spec = f"{base},jobs[{spec}]"
    return spec


def _fetch_level(url: str):
    separator = "&" if "?" in url else "?"
    return jenkins_client._fetch(
        f"{url}{separator}tree={_level_spec(JENKINS_SKELETON_MAX_DEPTH)}",
        timeout=JOBS_INDEX_TIMEOUT_SECONDS,
    )


def _job_entry(name: str, url: str, node: dict) -> JobEntry:
    last_build = node.get("lastBuild") or {}
    return JobEntry(
        name=name,
        url=url,
        status=last_build.get("result") or ("NOT_BUILT" if not last_build else None),
        build_number=last_build.get("number"),
        duration=last_build.get("duration"),
        timestamp=last_build.get("timestamp"),
    )


def _collect_views(node: dict, folder: str, views: list) -> None:
    for view in node.get("views") or []:
        name, url = view.get("name"), view.get("url")
        if name and url:
            views.append(
                ViewEntry(
                    name=name,
                    path=f"{folder}/{name}" if folder else name,
                    url=url,
                    folder=folder,
                )
            )


def _parse_children(node, prefix, depth_left, jobs, folders, views, unresolved) -> None:
    for entry in node.get("jobs") or []:
        name = entry.get("name")
        if not name:
            continue

        full_name = f"{prefix}/{name}" if prefix else name
        entry_class = (entry.get("_class") or "").lower()
        is_container = any(marker in entry_class for marker in CONTAINER_MARKERS)
        has_children = isinstance(entry.get("jobs"), list)

        _collect_views(entry, full_name, views)

        for branch in entry.get("branches") or []:
            branch_name, branch_url = branch.get("name"), branch.get("url")
            if branch_name and branch_url:
                jobs.append(_job_entry(f"{full_name}/{branch_name}", branch_url, branch))

        if is_container or has_children:
            folders.append(full_name)

        if has_children:
            _parse_children(
                entry, full_name, depth_left - 1, jobs, folders, views, unresolved
            )
        elif is_container:
            # Jenkins returned no children. Either the query did not reach this
            # depth (follow up) or the folder is genuinely empty (nothing to do).
            if depth_left <= 0:
                api_url = jenkins_client._job_api_url(entry)
                if api_url:
                    unresolved.append((api_url, full_name))
        else:
            jobs.append(_job_entry(full_name, entry.get("url"), entry))


def build_snapshot() -> Snapshot:
    """
    Walk the Jenkins tree and return a Snapshot. Any fetch failure or exhausted
    budget is recorded in `errors`, which clears `complete` -- callers use that
    to decide whether the result is safe to publish.
    """
    started_at = time.monotonic()
    deadline = started_at + JOBS_INDEX_TIMEOUT_SECONDS
    root_url = f"{(jenkins_client.JENKINS_URL or '').rstrip('/')}/api/json"

    jobs: list[JobEntry] = []
    folders: list[str] = []
    views: list[ViewEntry] = []
    errors: list[str] = []
    requests_made = 0

    frontier = [(root_url, "")]
    while frontier:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append(f"budget_exhausted_with_{len(frontier)}_folders_pending")
            break

        batch, frontier = frontier, []
        pool = ThreadPoolExecutor(max_workers=JENKINS_MAX_WORKERS)
        try:
            futures = {pool.submit(_fetch_level, url): (url, name) for url, name in batch}
            try:
                # The old walk used as_completed() with no timeout, so a level
                # ran to completion however long it took and the budget was
                # only consulted between levels.
                for future in as_completed(futures, timeout=max(remaining, 0.1)):
                    _, full_name = futures[future]
                    requests_made += 1
                    result = future.result()
                    if not result.ok:
                        errors.append(f"fetch_failed:{full_name or '<root>'}:{result.error}")
                        continue
                    node = result.data or {}
                    if not full_name:
                        _collect_views(node, "", views)
                    _parse_children(
                        node,
                        full_name,
                        JENKINS_SKELETON_MAX_DEPTH,
                        jobs,
                        folders,
                        views,
                        frontier,
                    )
            except FuturesTimeoutError:
                pending = sum(1 for future in futures if not future.done())
                errors.append(f"level_timeout_with_{pending}_requests_pending")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    snapshot = _finalize(jobs, folders, views, started_at, requests_made, errors)
    logger.info(
        "Jenkins index built jobs=%d folders=%d views=%d complete=%s seconds=%.2f requests=%d",
        len(snapshot.jobs),
        len(snapshot.folders),
        len(snapshot.views),
        snapshot.complete,
        snapshot.build_seconds,
        snapshot.requests_made,
    )
    return snapshot


# ---------------- PUBLICATION ----------------
_current: Snapshot | None = None
_generation = 0
_last_attempt_monotonic = 0.0
_refresh_lock = threading.Lock()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_stop_event = threading.Event()


def should_accept(new: Snapshot | None, current: Snapshot | None) -> bool:
    """
    Decide whether a freshly built snapshot may replace the published one.

    This is the rule the old cache lacked: a walk that came back empty because
    Jenkins was unreachable was cached as "there are no jobs", which blanked
    every tool for the length of the TTL.
    """
    if new is None:
        return False
    if current is None:
        # Nothing to lose, but refuse a snapshot that is both empty and known
        # incomplete -- that is a failed walk, not an empty Jenkins.
        return bool(new.jobs) or new.complete
    if not new.jobs and current.jobs:
        return False
    if new.complete:
        return True
    # Incomplete, and we already have something. Keep the known-good copy until
    # it is old enough that partial data genuinely beats it.
    return current.age_seconds > JOBS_INDEX_MAX_AGE_SECONDS


def refresh(force: bool = False) -> Snapshot | None:
    """
    Rebuild the index, single-flight. Concurrent callers coalesce onto one walk
    instead of each starting their own, which is how six simultaneous requests
    used to become six full walks.
    """
    global _current, _generation, _last_attempt_monotonic

    seen_generation = _generation
    with _refresh_lock:
        if not force:
            if _generation != seen_generation and _current is not None:
                return _current
            if (
                _last_attempt_monotonic
                and time.monotonic() - _last_attempt_monotonic
                < JOBS_INDEX_RETRY_COOLDOWN_SECONDS
            ):
                return _current

        _last_attempt_monotonic = time.monotonic()
        snapshot = build_snapshot()

        if should_accept(snapshot, _current):
            _current = snapshot
            _generation += 1
        else:
            logger.warning(
                "Jenkins index refresh rejected jobs=%d complete=%s errors=%s "
                "(keeping snapshot aged %.0fs)",
                len(snapshot.jobs),
                snapshot.complete,
                list(snapshot.errors[:3]),
                _current.age_seconds if _current else -1,
            )
        return _current


def current() -> Snapshot | None:
    return _current


def ensure() -> Snapshot | None:
    """
    Return the published snapshot, building one synchronously if none exists
    yet. Only the first request after startup can pay for a walk; the retry
    cooldown keeps a Jenkins outage from making every request wait.
    """
    snapshot = _current
    if snapshot is not None:
        return snapshot
    return refresh()


def _refresh_loop() -> None:
    try:
        refresh()
    except Exception:
        logger.exception("Jenkins index initial refresh failed")

    while not _stop_event.wait(JOBS_INDEX_REFRESH_SECONDS):
        try:
            refresh()
        except Exception:
            logger.exception("Jenkins index background refresh failed")


def start_background_refresh() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_refresh_loop, name="jenkins-index-refresh", daemon=True
        )
        _thread.start()
        logger.info(
            "Jenkins index background refresh started interval=%ds",
            JOBS_INDEX_REFRESH_SECONDS,
        )


def stop_background_refresh(timeout: float = 5.0) -> None:
    global _thread
    with _thread_lock:
        _stop_event.set()
        thread = _thread
        _thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def reset_for_tests() -> None:
    global _current, _generation, _last_attempt_monotonic
    stop_background_refresh()
    with _refresh_lock:
        _current = None
        _generation = 0
        _last_attempt_monotonic = 0.0


# ---------------- NAME RESOLUTION ----------------
_SEPARATORS = re.compile(r"[\s_.\-]+")


def normalize(value: str) -> str:
    """
    One normalization rule for every entity type. Today jobs and folders match
    case-sensitively while views match case-insensitively, so the same phrasing
    succeeds or fails depending on which tool the agent happened to pick.
    """
    return _SEPARATORS.sub(" ", (value or "").strip().casefold()).strip()


def _tokens(value: str) -> set[str]:
    return {token for token in normalize(value).replace("/", " ").split() if token}


def _score(query: str, candidate: str) -> int:
    return int(SequenceMatcher(None, query, candidate).ratio() * 100)


@dataclass(frozen=True)
class Resolution:
    """
    Three outcomes, never a bare empty list. "not_found" carries `candidates`
    as suggestions so the agent can ask instead of guessing, and the caller can
    report a bad name as a bad name rather than as "nothing here".
    """

    status: str
    value: str | None = None
    candidates: tuple[str, ...] = ()
    matched_by: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"


def resolve_name(query: str, known: tuple[str, ...]) -> Resolution:
    if not query or not query.strip():
        return Resolution("not_found")
    if not known:
        return Resolution("not_found")

    normalized_query = normalize(query)
    query_tokens = _tokens(query)

    tiers = [
        ("exact", [name for name in known if name == query]),
        ("exact_normalized", [name for name in known if normalize(name) == normalized_query]),
        (
            "segment",
            [
                name
                for name in known
                if normalize(name.split("/")[-1]) == normalized_query
            ],
        ),
        (
            "tokens",
            [name for name in known if query_tokens and query_tokens <= _tokens(name)],
        ),
    ]

    for matched_by, matches in tiers:
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            return Resolution("resolved", unique[0], matched_by=matched_by)
        if unique:
            return Resolution(
                "ambiguous",
                candidates=tuple(sorted(unique)[:MAX_CANDIDATES]),
                matched_by=matched_by,
            )

    # No deterministic tier matched, so fall back to similarity. A fuzzy hit is
    # only acted on when it looks like a typo of one specific name: strong on its
    # own and clearly ahead of the runner-up. Anything weaker becomes a
    # suggestion, because silently picking the nearest string is how a tool ends
    # up confidently answering a question nobody asked.
    scored = sorted(
        (
            (
                max(
                    _score(normalized_query, normalize(name)),
                    _score(normalized_query, normalize(name.split("/")[-1])),
                ),
                name,
            )
            for name in known
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )

    strong = [name for score, name in scored if score >= FUZZY_AUTO_RESOLVE_SCORE]
    if len(strong) == 1:
        best_score = scored[0][0]
        runner_up = scored[1][0] if len(scored) > 1 else 0
        if best_score - runner_up >= FUZZY_MARGIN:
            return Resolution("resolved", strong[0], matched_by="fuzzy")
    if len(strong) > 1:
        return Resolution(
            "ambiguous", candidates=tuple(strong[:MAX_CANDIDATES]), matched_by="fuzzy"
        )

    suggestions = [name for score, name in scored if score >= FUZZY_MIN_SCORE]
    return Resolution(
        "not_found",
        candidates=tuple((suggestions or [name for _, name in scored])[:5]),
    )


def resolve_folder(query: str, snapshot: Snapshot) -> Resolution:
    return resolve_name(query, snapshot.folders)


def resolve_job(query: str, snapshot: Snapshot) -> Resolution:
    return resolve_name(query, tuple(job.name for job in snapshot.jobs))


def resolve_view(query: str, snapshot: Snapshot) -> Resolution:
    by_path = resolve_name(query, tuple(view.path for view in snapshot.views))
    if by_path.status != "not_found":
        return by_path
    return resolve_name(query, tuple(view.name for view in snapshot.views))
