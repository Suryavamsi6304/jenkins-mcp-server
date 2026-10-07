import time
import unittest
from unittest.mock import patch

import app.jenkins_client as jenkins_client
from app import tools


class MapConcurrentlyTests(unittest.TestCase):
    def test_slow_items_are_truncated_when_deadline_passes(self):
        def fetch(item):
            if item == "slow":
                time.sleep(0.2)
            return item

        deadline = time.monotonic() + 0.02
        results, truncated = tools._map_concurrently(fetch, ["fast", "slow"], deadline=deadline)

        self.assertTrue(truncated)
        self.assertEqual("fast", results[0])

    def test_no_deadline_waits_for_all_results(self):
        results, truncated = tools._map_concurrently(lambda item: item * 2, [1, 2, 3])

        self.assertFalse(truncated)
        self.assertEqual([2, 4, 6], results)


# The former SkeletonStatusReuseTests covered get_all_jobs_status walking
# Jenkins per request and falling back to a per-job get_latest_build. That tool
# now reads the background index instead and never issues a per-job request, so
# those assertions no longer describe the design. Replacements live in
# tests/test_index.py::GetAllJobsTests -- in particular
# test_served_requests_do_not_hit_jenkins (no live fetch at all) and
# test_never_built_job_is_reported_not_built_rather_than_dropped (absent
# lastBuild means NOT_BUILT rather than a second Jenkins round trip).


class CacheOptimizationTests(unittest.TestCase):
    def setUp(self):
        jenkins_client._job_lookup_cache.clear()
        jenkins_client._failed_jobs_cache.clear()
        tools._metrics_cache.clear()
        tools._metrics_cache_expires_at = 0

    def test_find_job_by_name_uses_lookup_cache(self):
        job = {"name": "Payments/job-a", "url": "https://jenkins.example.test/job/Payments/job/job-a/"}
        jenkins_client._job_lookup_cache["Payments/job-a"] = job

        with patch("app.jenkins_client.get_all_jobs_recursive") as get_all_jobs_recursive:
            result = jenkins_client.find_job_by_name("job-a")

        get_all_jobs_recursive.assert_not_called()
        self.assertEqual(job, result)

    def test_get_failed_jobs_uses_failed_jobs_cache(self):
        expected = [{"job": "Payments/job-a", "status": "FAILURE"}]
        jenkins_client._failed_jobs_cache.extend(expected)

        with patch("app.tools.get_all_jobs_status") as get_all_jobs_status:
            result = tools.get_failed_jobs()

        get_all_jobs_status.assert_not_called()
        self.assertEqual(expected, result["failed_jobs"])

    def test_get_jenkins_metrics_uses_metric_cache(self):
        cached = {"metric": "success_count", "value": 42, "unit": "count"}
        tools._metrics_cache[("success_count", None, None, None, None, None, None, "count")] = cached
        tools._metrics_cache_expires_at = time.monotonic() + 60

        with patch("app.tools.get_all_jobs_recursive") as get_all_jobs_recursive:
            result = tools.get_jenkins_metrics("success_count")

        get_all_jobs_recursive.assert_not_called()
        self.assertEqual(cached, result)


class BuildHistoryLimitTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "name": "Payments/job-a",
            "url": "https://jenkins.example.test/job/Payments/job/job-a/",
        }

    @staticmethod
    def _builds(first_number, count):
        return [
            {
                "number": first_number - offset,
                "timestamp": (first_number - offset) * 1000,
                "result": "SUCCESS",
            }
            for offset in range(count)
        ]

    def test_history_defaults_to_configured_build_limit(self):
        page = {"builds": self._builds(20, tools.MAX_BUILDS_PER_JOB)}
        with patch("app.tools.find_job_by_name", return_value=self.job), patch(
            "app.tools.get_builds_in_range", return_value=page
        ) as get_page:
            result = tools.get_build_history("job-a")

        self.assertEqual(tools.MAX_BUILDS_PER_JOB, result["build_count"])
        self.assertEqual(tools.MAX_BUILDS_PER_JOB, get_page.call_args.args[2])

    def test_explicit_history_limit_caps_returned_builds(self):
        pages = [
            {"builds": self._builds(20, 2)},
            {"builds": self._builds(18, 2)},
        ]
        with patch("app.tools.find_job_by_name", return_value=self.job), patch(
            "app.tools.get_builds_in_range", side_effect=pages
        ):
            result = tools.get_build_history("job-a", page_size=2)

        self.assertEqual(2, result["build_count"])

    def test_global_history_forwards_folder_scope(self):
        with patch("app.tools.get_all_jobs_recursive", return_value=[]) as get_jobs:
            result = tools.get_build_history("*", folder_name="Payments")

        self.assertEqual(0, result["build_count"])
        get_jobs.assert_called_once_with(folder_name="Payments")