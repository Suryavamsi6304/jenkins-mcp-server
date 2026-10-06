import unittest
from unittest.mock import patch

from app import jenkins_client


class StreamResponse:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.chunks = chunks
        self.consumed_chunks = 0
        self.closed = False

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            self.consumed_chunks += 1
            yield chunk

    def close(self):
        self.closed = True


class JenkinsDestinationTests(unittest.TestCase):
    def setUp(self):
        self.original_jenkins_url = jenkins_client.JENKINS_URL
        jenkins_client.JENKINS_URL = "https://jenkins.example.test"

    def tearDown(self):
        jenkins_client.JENKINS_URL = self.original_jenkins_url

    def test_only_configured_jenkins_origin_is_trusted(self):
        self.assertTrue(
            jenkins_client._is_trusted_jenkins_url(
                "https://jenkins.example.test/job/example"
            )
        )

        for target in (
            "http://jenkins.example.test/job/example",
            "https://jenkins.example.test:8443/job/example",
            "https://example.test/job/example",
            "http://169.254.169.254/latest/meta-data",
            "https://jenkins.example.test@evil.example/job/example",
        ):
            with self.subTest(target=target):
                self.assertFalse(jenkins_client._is_trusted_jenkins_url(target))

    def test_untrusted_url_is_not_requested(self):
        with patch("app.jenkins_client.requests.get") as request_get:
            result = jenkins_client._safe_get("http://169.254.169.254/latest/meta-data")

        self.assertIsNone(result)
        request_get.assert_not_called()

    def test_view_url_is_treated_as_an_identifier_not_a_destination(self):
        api_url = jenkins_client._view_api_url("https://attacker.example/view")

        self.assertTrue(api_url.startswith("https://jenkins.example.test/view/"))
        self.assertIn("attacker.example", api_url)

    def test_find_view_by_name_resolves_folder_views_without_job_recursion(self):
        root_response = {
            "jobs": [{
                "name": "DevOps_Jenkins_Job",
                "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                "url": "https://jenkins.example.test/job/DevOps_Jenkins_Job/",
            }],
            "views": [],
        }
        folder_response = {
            "views": [{
                "name": "DevOps_Automation_Jobs",
                "url": "https://jenkins.example.test/job/DevOps_Jenkins_Job/view/DevOps_Automation_Jobs/",
            }]
        }
        view_jobs = {
            "name": "DevOps_Automation_Jobs",
            "jobs": [{"name": "Demo-Job", "url": "https://jenkins.example.test/job/DevOps_Jenkins_Job/job/Demo-Job/"}],
        }

        with patch(
            "app.jenkins_client._safe_get",
            side_effect=[root_response, folder_response, view_jobs],
        ) as safe_get:
            result = jenkins_client.get_jobs_in_view("devops_automation_jobs")

        self.assertEqual(1, result["job_count"])
        self.assertEqual("Demo-Job", result["jobs"][0]["name"])
        self.assertEqual(3, safe_get.call_count)
        self.assertIn("/api/json?tree=jobs[name,url,_class],views[name,url]", safe_get.call_args_list[0].args[0])
        self.assertIn("/job/DevOps_Jenkins_Job/api/json?tree=views[name,url]", safe_get.call_args_list[1].args[0])
        self.assertTrue(safe_get.call_args_list[2].args[0].startswith("https://jenkins.example.test/job/DevOps_Jenkins_Job/view/DevOps_Automation_Jobs/"))

    def test_build_history_uses_jenkins_range_pages(self):
        builds = [
            {"number": number, "timestamp": number}
            for number in range(4, 0, -1)
        ]
        cache = {}

        with patch(
            "app.jenkins_client._safe_get",
            side_effect=[
                {"builds": builds[:2]},
                {"builds": builds[2:]},
            ],
        ) as safe_get:
            first_page = jenkins_client.get_builds_in_range(
                "https://jenkins.example.test/job/example/",
                0,
                2,
                cache=cache,
            )
            second_page = jenkins_client.get_builds_in_range(
                "https://jenkins.example.test/job/example/",
                2,
                2,
                cache=cache,
            )

        self.assertEqual([4, 3], [build["number"] for build in first_page["builds"]])
        self.assertEqual([2, 1], [build["number"] for build in second_page["builds"]])
        self.assertEqual(2, safe_get.call_count)
        self.assertIn("builds[number,id,url,result,duration,timestamp]{0,2}", safe_get.call_args_list[0].args[0])
        self.assertIn("builds[number,id,url,result,duration,timestamp]{2,2}", safe_get.call_args_list[1].args[0])

    def test_build_history_falls_back_when_jenkins_repeats_first_range(self):
        builds = [
            {"number": number, "timestamp": number}
            for number in range(4, 0, -1)
        ]
        cache = {}
        first_page = {"builds": builds[:2]}

        with patch(
            "app.jenkins_client._safe_get",
            side_effect=[first_page, first_page, {"allBuilds": builds}],
        ) as safe_get:
            jenkins_client.get_builds_in_range(
                "https://jenkins.example.test/job/example/",
                0,
                2,
                cache=cache,
            )
            second_page = jenkins_client.get_builds_in_range(
                "https://jenkins.example.test/job/example/",
                2,
                2,
                cache=cache,
            )

        self.assertEqual([2, 1], [build["number"] for build in second_page["builds"]])
        self.assertEqual(3, safe_get.call_count)
        self.assertIn("allBuilds[", safe_get.call_args_list[2].args[0])

    def test_range_request_failure_does_not_trigger_a_second_timeout(self):
        with patch("app.jenkins_client._safe_get", return_value=None) as safe_get:
            result = jenkins_client.get_builds_in_range(
                "https://jenkins.example.test/job/example/",
                0,
                10,
                cache={},
            )

        self.assertIsNone(result)
        safe_get.assert_called_once()

    def test_latest_build_uses_one_targeted_request(self):
        job_data = {
            "fullName": "Folder/example",
            "url": "https://jenkins.example.test/job/Folder/job/example/",
            "lastBuild": {
                "number": 12,
                "url": "https://jenkins.example.test/job/Folder/job/example/12/",
                "result": "SUCCESS",
                "duration": 42000,
                "timestamp": 1000,
            },
        }

        with patch("app.jenkins_client._safe_get", return_value=job_data) as safe_get:
            result = jenkins_client.get_latest_build(job_data["url"])

        self.assertEqual(12, result["build_number"])
        self.assertEqual("SUCCESS", result["status"])
        safe_get.assert_called_once()
        self.assertIn("lastBuild[", safe_get.call_args.args[0])

    def test_folder_scoped_discovery_fetches_only_that_folder(self):
        jenkins_client._jobs_cache["data"] = None
        jenkins_client._jobs_cache["expires_at"] = 0
        jenkins_client._folder_jobs_cache.clear()

        folder_response = {
            "jobs": [
                {
                    "name": "job-a",
                    "_class": "hudson.model.FreeStyleProject",
                    "url": "https://jenkins.example.test/job/Wanted/job/job-a/",
                    "lastBuild": {"number": 1, "result": "SUCCESS", "duration": 500, "timestamp": 500},
                }
            ]
        }

        with patch("app.jenkins_client._safe_get", return_value=folder_response) as safe_get:
            jobs = jenkins_client.get_all_jobs_recursive(folder_name="Wanted")

        self.assertEqual(["Wanted/job-a"], [job["name"] for job in jobs])
        safe_get.assert_called_once()
        self.assertTrue(
            safe_get.call_args.args[0].startswith(
                "https://jenkins.example.test/job/Wanted/api/json?tree="
            )
        )

    def test_discovery_resolves_a_nested_tree_in_a_single_request(self):
        jenkins_client._jobs_cache["data"] = None
        jenkins_client._jobs_cache["expires_at"] = 0
        jenkins_client._folder_jobs_cache.clear()

        root_response = {
            "jobs": [
                {
                    "name": f"Folder{i}",
                    "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                    "url": f"https://jenkins.example.test/job/Folder{i}/",
                    "jobs": [
                        {
                            "name": "job-a",
                            "_class": "hudson.model.FreeStyleProject",
                            "url": f"https://jenkins.example.test/job/Folder{i}/job/job-a/",
                            "lastBuild": {"number": 3, "result": "SUCCESS", "duration": 1000, "timestamp": 1000},
                            "builds": [{"number": 3, "result": "SUCCESS", "duration": 1000, "timestamp": 1000}],
                        }
                    ],
                }
                for i in range(5)
            ]
        }

        with patch("app.jenkins_client._safe_get", return_value=root_response) as safe_get:
            jobs = jenkins_client.get_all_jobs_recursive()

        self.assertFalse(jobs.truncated)
        self.assertEqual(5, len(jobs))
        self.assertEqual(
            {f"Folder{i}/job-a" for i in range(5)},
            {job["name"] for job in jobs},
        )
        self.assertTrue(all(job["status_known"] for job in jobs))
        safe_get.assert_called_once()

    def test_discovery_follows_up_when_depth_limit_is_reached(self):
        jenkins_client._jobs_cache["data"] = None
        jenkins_client._jobs_cache["expires_at"] = 0
        jenkins_client._folder_jobs_cache.clear()

        root_response = {
            "jobs": [
                {
                    "name": "Deep",
                    "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                    "url": "https://jenkins.example.test/job/Deep/",
                }
            ]
        }
        followup_response = {
            "jobs": [
                {
                    "name": "job-a",
                    "_class": "hudson.model.FreeStyleProject",
                    "url": "https://jenkins.example.test/job/Deep/job/job-a/",
                    "lastBuild": {"number": 7, "result": "FAILURE", "duration": 200, "timestamp": 200},
                }
            ]
        }

        with patch(
            "app.jenkins_client._safe_get",
            side_effect=[root_response, followup_response],
        ) as safe_get, patch("app.jenkins_client.JENKINS_SKELETON_MAX_DEPTH", 0):
            jobs = jenkins_client.get_all_jobs_recursive()

        self.assertEqual(["Deep/job-a"], [job["name"] for job in jobs])
        self.assertEqual(2, safe_get.call_count)

    def test_discovery_marks_truncated_and_skips_caching_when_budget_exceeded(self):
        jenkins_client._jobs_cache["data"] = None
        jenkins_client._jobs_cache["expires_at"] = 0
        jenkins_client._folder_jobs_cache.clear()

        root_response = {
            "jobs": [
                {
                    "name": "Folder0",
                    "url": "https://jenkins.example.test/job/Folder0/",
                    "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                }
            ]
        }

        with patch("app.jenkins_client._safe_get", return_value=root_response), patch(
            "app.jenkins_client.JENKINS_DISCOVERY_TIMEOUT_SECONDS", 0
        ):
            jobs = jenkins_client.get_all_jobs_recursive()

        self.assertTrue(jobs.truncated)
        self.assertIsNone(jenkins_client._jobs_cache["data"])

    def test_build_log_uses_supported_range_tail(self):
        response = StreamResponse(
            206,
            [b"tail bytes"],
            {"Content-Range": "bytes 90-99/100", "Content-Length": "10"},
        )
        with patch("app.jenkins_client.MAX_CONSOLE_LOG_BYTES", 10), patch(
            "app.jenkins_client.USERNAME", "user"
        ), patch("app.jenkins_client.API_TOKEN", "token"), patch(
            "app.jenkins_client._session.get", return_value=response
        ) as session_get:
            result = jenkins_client.get_build_log(
                "https://jenkins.example.test/job/example/",
                12,
            )

        self.assertIn("showing the tail", result)
        self.assertIn("tail bytes", result)
        self.assertEqual("bytes=-10", session_get.call_args.kwargs["headers"]["Range"])
        self.assertTrue(response.closed)

    def test_build_log_stops_reading_when_range_is_ignored(self):
        response = StreamResponse(
            200,
            [b"12345678", b"abcdefgh", b"unread"],
            {"Content-Length": "22"},
        )
        with patch("app.jenkins_client.MAX_CONSOLE_LOG_BYTES", 10), patch(
            "app.jenkins_client.USERNAME", "user"
        ), patch("app.jenkins_client.API_TOKEN", "token"), patch(
            "app.jenkins_client._session.get", return_value=response
        ):
            result = jenkins_client.get_build_log(
                "https://jenkins.example.test/job/example/",
                12,
            )

        self.assertIn("showing the beginning", result)
        self.assertIn("12345678ab", result)
        self.assertNotIn("unread", result)
        self.assertEqual(2, response.consumed_chunks)
        self.assertTrue(response.closed)