import unittest

from app.api import build_report, close_issue
from app.cache import SimpleCache
from app.data import ISSUES
from app.repository import IssueRepository
from app.service import IssueService


class IssueTrackerRegressionTests(unittest.TestCase):
    def setUp(self):
        seed = {issue_id: payload.copy() for issue_id, payload in ISSUES.items()}
        self.service = IssueService(IssueRepository(seed), SimpleCache())

    def test_default_limit_comes_from_env(self):
        titles = self.service.list_titles("open")
        self.assertEqual(len(titles), 3)

    def test_status_filter_is_case_insensitive(self):
        titles = self.service.list_titles(" OPEN ")
        self.assertEqual(len(titles), 3)
        self.assertEqual(titles[0], "Login button broken")

    def test_close_issue_invalidates_cached_summary(self):
        before = self.service.summary("open")
        self.assertIn("Login button broken", before)
        self.service.close_issue("I-100")
        after_open = self.service.summary("open")
        self.assertNotIn("Login button broken", after_open)
        after_closed = self.service.summary("closed")
        self.assertIn("Login button broken", after_closed)


class ApiSmokeTests(unittest.TestCase):
    def test_build_report_runs(self):
        self.assertIn("count=", build_report("open"))
        close_issue("I-100")
        self.assertIn("count=", build_report("closed"))


if __name__ == "__main__":
    unittest.main()
