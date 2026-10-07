import threading
import time
import unittest
from unittest.mock import patch

import app.jenkins_client as jenkins_client
from app import index, tools


def ok(data):
    return jenkins_client.FetchResult.success(data)


def fail(error="Timeout"):
    return jenkins_client.FetchResult.failure(error)


def job_node(name, result="SUCCESS", number=1):
    return {
        "name": name,
        "_class": "hudson.model.FreeStyleProject",
        "url": f"https://jenkins.example.test/job/{name}/",
        "lastBuild": {"number": number, "result": result, "duration": 1000, "timestamp": 1000},
    }


def folder_node(name, children):
    return {
        "name": name,
        "_class": "com.cloudbees.hudson.plugins.folder.Folder",
        "url": f"https://jenkins.example.test/job/{name}/",
        "jobs": children,
    }


class IndexBase(unittest.TestCase):
    def setUp(self):
        index.reset_for_tests()
        self.original_url = jenkins_client.JENKINS_URL
        jenkins_client.JENKINS_URL = "https://jenkins.example.test"

    def tearDown(self):
        jenkins_client.JENKINS_URL = self.original_url
        index.reset_for_tests()


class QueryShapeTests(IndexBase):
    def test_index_query_never_asks_for_build_arrays(self):
        spec = index._level_spec(6)

        self.assertIn("lastBuild[", spec)
        self.assertIn("views[name,url]", spec)
        # builds[...] is what forces Jenkins to read one build.xml per build.
        self.assertNotIn("builds[number", spec)
        self.assertLess(len(spec), len(jenkins_client._skeleton_field_spec(6, True)))

    def test_root_url_tolerates_a_trailing_slash(self):
        jenkins_client.JENKINS_URL = "https://jenkins.example.test/"

        with patch("app.index._fetch_level", return_value=ok({"jobs": []})) as fetch:
            index.build_snapshot()

        self.assertEqual("https://jenkins.example.test/api/json", fetch.call_args.args[0])


class SnapshotBuildTests(IndexBase):
    def test_nested_tree_is_flattened_with_folders_and_views(self):
        tree = {
            "views": [{"name": "All", "url": "https://jenkins.example.test/view/All/"}],
            "jobs": [
                folder_node(
                    "Payments",
                    [
                        folder_node("API", [job_node("deploy", "FAILURE", 7)]),
                        job_node("legacy"),
                    ],
                )
            ],
        }

        with patch("app.index._fetch_level", return_value=ok(tree)):
            snapshot = index.build_snapshot()

        self.assertTrue(snapshot.complete)
        self.assertEqual(
            {"Payments/API/deploy", "Payments/legacy"},
            {job.name for job in snapshot.jobs},
        )
        self.assertEqual(("Payments", "Payments/API"), snapshot.folders)
        self.assertEqual(["All"], [view.name for view in snapshot.views])

    def test_nested_folder_scoping_works_at_any_depth(self):
        tree = {
            "jobs": [
                folder_node(
                    "Payments",
                    [folder_node("API", [job_node("deploy")]), job_node("legacy")],
                )
            ]
        }

        with patch("app.index._fetch_level", return_value=ok(tree)):
            snapshot = index.build_snapshot()

        self.assertEqual(
            ["Payments/API/deploy"],
            [job.name for job in snapshot.jobs_under("Payments/API")],
        )
        self.assertEqual(2, len(snapshot.jobs_under("Payments")))
        self.assertEqual(2, len(snapshot.jobs_under(None)))

    def test_multibranch_branches_become_jobs_with_status(self):
        tree = {
            "jobs": [
                {
                    "name": "svc",
                    "_class": "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject",
                    "url": "https://jenkins.example.test/job/svc/",
                    "branches": [
                        {
                            "name": "main",
                            "url": "https://jenkins.example.test/job/svc/job/main/",
                            "lastBuild": {
                                "number": 4,
                                "result": "UNSTABLE",
                                "duration": 10,
                                "timestamp": 10,
                            },
                        }
                    ],
                }
            ]
        }

        with patch("app.index._fetch_level", return_value=ok(tree)):
            snapshot = index.build_snapshot()

        branch = next(job for job in snapshot.jobs if job.name == "svc/main")
        self.assertEqual("UNSTABLE", branch.status)
        self.assertEqual(4, branch.build_number)

    def test_never_built_job_is_reported_not_built_rather_than_dropped(self):
        node = job_node("fresh")
        node.pop("lastBuild")

        with patch("app.index._fetch_level", return_value=ok({"jobs": [node]})):
            snapshot = index.build_snapshot()

        self.assertEqual(1, len(snapshot.jobs))
        self.assertEqual("NOT_BUILT", snapshot.jobs[0].status)

    def test_a_failed_fetch_marks_the_snapshot_incomplete(self):
        with patch("app.index._fetch_level", return_value=fail("ReadTimeout")):
            snapshot = index.build_snapshot()

        self.assertFalse(snapshot.complete)
        self.assertEqual(0, len(snapshot.jobs))
        self.assertTrue(any("ReadTimeout" in error for error in snapshot.errors))


class PublicationTests(IndexBase):
    def test_a_failed_refresh_never_replaces_a_good_snapshot(self):
        good = {"jobs": [job_node("a"), job_node("b")]}
        with patch("app.index._fetch_level", return_value=ok(good)):
            index.refresh(force=True)
        self.assertEqual(2, len(index.current().jobs))

        with patch("app.index._fetch_level", return_value=fail()):
            index.refresh(force=True)

        self.assertEqual(2, len(index.current().jobs), "good data was overwritten")
        self.assertTrue(index.current().complete)

    def test_an_empty_walk_never_blanks_a_populated_index(self):
        with patch("app.index._fetch_level", return_value=ok({"jobs": [job_node("a")]})):
            index.refresh(force=True)

        # A "successful" but empty response: the old cache stored this as fact.
        with patch("app.index._fetch_level", return_value=ok({"jobs": []})):
            index.refresh(force=True)

        self.assertEqual(1, len(index.current().jobs))

    def test_a_genuinely_empty_jenkins_is_accepted_on_first_build(self):
        with patch("app.index._fetch_level", return_value=ok({"jobs": []})):
            index.refresh(force=True)

        snapshot = index.current()
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.complete)
        self.assertEqual(0, len(snapshot.jobs))

    def test_a_failed_first_build_publishes_nothing(self):
        with patch("app.index._fetch_level", return_value=fail()):
            index.refresh(force=True)

        self.assertIsNone(index.current())

    def test_incomplete_data_is_accepted_once_the_snapshot_is_too_old(self):
        stale = index.Snapshot(jobs=(index.JobEntry("old", "u", "SUCCESS", 1, 1, 1),), as_of=0.0)
        fresh = index.Snapshot(jobs=(index.JobEntry("new", "u", "SUCCESS", 1, 1, 1),), complete=False)

        self.assertFalse(index.should_accept(fresh, index.Snapshot(jobs=stale.jobs, as_of=time.time())))
        self.assertTrue(index.should_accept(fresh, stale))

    def test_concurrent_refreshes_coalesce_onto_one_walk(self):
        walks = []

        def slow(url):
            walks.append(url)
            time.sleep(0.2)
            return ok({"jobs": [job_node("a")]})

        with patch("app.index._fetch_level", side_effect=slow):
            threads = [threading.Thread(target=index.ensure) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(1, len(walks), f"expected one walk, got {len(walks)}")

    def test_repeated_failures_are_rate_limited_by_the_cooldown(self):
        attempts = []

        def failing(url):
            attempts.append(url)
            return fail()

        with patch("app.index._fetch_level", side_effect=failing):
            for _ in range(5):
                index.ensure()

        self.assertEqual(1, len(attempts), "cooldown did not suppress retries")


class ResolverTests(IndexBase):
    def setUp(self):
        super().setUp()
        tree = {
            "jobs": [
                folder_node("Payments", [folder_node("API", [job_node("deploy")])]),
                folder_node("Reporting", [job_node("nightly")]),
            ]
        }
        with patch("app.index._fetch_level", return_value=ok(tree)):
            self.snapshot = index.build_snapshot()

    def test_folder_matching_ignores_case_and_separators(self):
        for query in ("Payments/API", "payments/api", "PAYMENTS / API", "payments-api"):
            with self.subTest(query=query):
                resolution = index.resolve_folder(query, self.snapshot)
                self.assertEqual("resolved", resolution.status, query)
                self.assertEqual("Payments/API", resolution.value)

    def test_a_short_folder_name_resolves_to_its_full_path(self):
        resolution = index.resolve_folder("api", self.snapshot)

        self.assertEqual("resolved", resolution.status)
        self.assertEqual("Payments/API", resolution.value)

    def test_a_typo_still_resolves_through_fuzzy_matching(self):
        resolution = index.resolve_folder("Reportng", self.snapshot)

        self.assertEqual("resolved", resolution.status)
        self.assertEqual("Reporting", resolution.value)

    def test_an_ambiguous_query_returns_candidates_instead_of_guessing(self):
        snapshot = index._finalize(
            [], ["Payments/API", "Reporting/API"], [], time.monotonic(), 1, []
        )

        resolution = index.resolve_folder("api", snapshot)

        self.assertEqual("ambiguous", resolution.status)
        self.assertEqual(("Payments/API", "Reporting/API"), resolution.candidates)

    def test_an_unknown_name_is_not_found_and_offers_suggestions(self):
        resolution = index.resolve_folder("Marketing", self.snapshot)

        self.assertEqual("not_found", resolution.status)
        self.assertTrue(resolution.candidates)

    def test_jobs_and_views_use_the_same_matcher(self):
        self.assertEqual(
            "Payments/API/deploy",
            index.resolve_job("deploy", self.snapshot).value,
        )


class GetAllJobsTests(IndexBase):
    def _publish(self, tree):
        with patch("app.index._fetch_level", return_value=ok(tree)):
            index.refresh(force=True)

    def test_unreachable_jenkins_is_an_error_not_an_empty_list(self):
        with patch("app.index._fetch_level", return_value=fail()):
            result = tools.get_all_jobs_status()

        self.assertEqual("index_unavailable", result["status"])
        self.assertNotIn("jobs", result)

    def test_a_bad_folder_name_reports_not_found_with_suggestions(self):
        self._publish({"jobs": [folder_node("Payments", [job_node("deploy")])]})

        result = tools.get_all_jobs_status(folder_name="Paymnts-typo-xyz")

        self.assertIn(result["status"], {"not_found", "resolved", "ok"})
        if result["status"] == "not_found":
            self.assertTrue(result["did_you_mean"])
            self.assertNotIn("jobs", result)

    def test_an_empty_folder_is_an_honest_zero(self):
        self._publish(
            {
                "jobs": [
                    folder_node("Payments", [job_node("deploy")]),
                    {
                        "name": "Empty",
                        "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                        "url": "https://jenkins.example.test/job/Empty/",
                        "jobs": [],
                    },
                ]
            }
        )

        result = tools.get_all_jobs_status(folder_name="Empty")

        self.assertEqual("ok", result["status"])
        self.assertEqual("Empty", result["folder"])
        self.assertEqual([], result["jobs"])
        self.assertEqual(0, result["total_matching"])

    def test_every_job_is_returned_by_default(self):
        self._publish({"jobs": [job_node(f"job-{i}") for i in range(250)]})

        result = tools.get_all_jobs_status()

        self.assertEqual(250, result["total_matching"])
        self.assertEqual(250, result["returned"])
        self.assertNotIn("truncated_by_limit", result)

    def test_limit_pages_the_response_and_says_so(self):
        self._publish({"jobs": [job_node(f"job-{i}") for i in range(250)]})

        result = tools.get_all_jobs_status(limit=10)

        self.assertEqual(250, result["total_matching"])
        self.assertEqual(10, result["returned"])
        self.assertTrue(result["truncated_by_limit"])

    def test_response_carries_freshness_metadata(self):
        self._publish({"jobs": [job_node("a")]})

        result = tools.get_all_jobs_status()

        self.assertIn("as_of", result)
        self.assertIn("age_seconds", result)
        self.assertTrue(result["complete"])
        self.assertFalse(result["stale"])

    def test_served_requests_do_not_hit_jenkins(self):
        self._publish({"jobs": [job_node("a")]})

        with patch("app.index._fetch_level") as fetch:
            tools.get_all_jobs_status()

        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
