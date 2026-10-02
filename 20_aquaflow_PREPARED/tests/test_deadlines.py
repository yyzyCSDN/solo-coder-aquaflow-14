import unittest
from datetime import date, timedelta

from aquaflow.regulatory.deadlines import (
    BLOCKED,
    DUE_SOON,
    OVERDUE,
    SCHEDULED,
    CLOSED,
    MANDATORY,
    PERIODIC,
    Cycle,
    DeadlineCalendar,
    Evidence,
    RegCase,
    SeverityRule,
)


def calendar(warn_days=7):
    return DeadlineCalendar(
        severity_rules=[SeverityRule("critical", 10), SeverityRule("major", 30)],
        cycles=[Cycle("monthly", 30)],
        warn_days=warn_days,
    )


class DeadlineBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.cal = calendar()
        self.cal.register_case(RegCase("C1", "critical", date(2026, 1, 1), "monthly"))
        # mandatory due: opened + 10 days = 2026-01-11

    def test_day_before_deadline_is_upcoming(self):
        self.assertEqual(self.cal.status("C1", date(2026, 1, 10)), DUE_SOON)
        self.assertEqual(self.cal.overdue_kinds("C1", date(2026, 1, 10)), ())

    def test_due_date_itself_is_still_upcoming_not_overdue(self):
        entry = self.cal.entry("C1", date(2026, 1, 11))
        self.assertEqual(entry.status, DUE_SOON)
        self.assertEqual(entry.overdue_kinds, ())
        self.assertNotIn("C1", [e.case_id for e in self.cal.views(date(2026, 1, 11))[OVERDUE]])

    def test_day_after_deadline_is_overdue(self):
        self.assertEqual(self.cal.status("C1", date(2026, 1, 12)), OVERDUE)
        self.assertEqual(self.cal.overdue_kinds("C1", date(2026, 1, 12)), (MANDATORY,))

    def test_far_from_deadline_is_scheduled(self):
        self.assertEqual(self.cal.status("C1", date(2026, 1, 3)), SCHEDULED)


class EvidenceRevisionTests(unittest.TestCase):
    def setUp(self):
        self.cal = calendar()
        self.cal.register_case(RegCase("C1", "major", date(2026, 1, 1), "monthly"))
        # Mandatory window closes 2026-01-31; satisfy it so periodic-only tests
        # can assert on the periodic deadline in isolation.
        self.cal.file_report("C1", MANDATORY, date(2026, 1, 20))

    def test_revision_appends_new_result_and_old_result_is_immutable(self):
        r0 = self.cal.latest_result("C1")
        self.assertIsNone(r0.periodic_due)

        e1 = self.cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        self.assertEqual(e1.periodic_due, date(2026, 2, 4))  # Jan 5 + 30
        self.assertEqual(self.cal.current_evidence("C1").revision, 1)

        self.cal.record_evidence(
            Evidence("E2", "C1", revision=2, accepted_at=date(2026, 1, 10), supersedes=1)
        )
        self.assertEqual(self.cal.latest_result("C1").periodic_due, date(2026, 2, 9))
        self.assertEqual(self.cal.current_evidence("C1").revision, 2)

        # Ledger is append-only: every prior calculation is retained unchanged.
        results = self.cal.results("C1")
        self.assertEqual(len(results), 3)
        self.assertIsNone(results[0].periodic_due)
        self.assertEqual(results[1].periodic_due, date(2026, 2, 4))
        self.assertEqual(results[2].periodic_due, date(2026, 2, 9))
        self.assertEqual(results[2].supersedes_result, results[1].result_id)
        with self.assertRaises(Exception):
            results[1].periodic_due = date(2026, 3, 1)

    def test_superseded_revision_no_longer_anchors(self):
        self.cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        self.cal.record_evidence(
            Evidence("E2", "C1", revision=2, accepted_at=date(2026, 1, 20), supersedes=1)
        )
        # The current periodic deadline follows E2, not the older E1.
        self.assertEqual(self.cal.latest_result("C1").anchor_evidence, "E2")
        self.assertEqual(self.cal.latest_result("C1").periodic_due, date(2026, 2, 19))

    def test_pending_revision_covers_old_evidence_until_accepted(self):
        self.cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        # A revision still under review covers nothing: anchor stays E1 ...
        self.cal.record_evidence(Evidence("E2", "C1", revision=2, supersedes=1))
        self.assertEqual(self.cal.current_evidence("C1").revision, 1)
        self.assertEqual(self.cal.latest_result("C1").periodic_due, date(2026, 2, 4))
        # ... and accepting the review recomputes from E2.
        self.cal.complete_review("C1", "E2", date(2026, 1, 12))
        self.assertEqual(self.cal.current_evidence("C1").revision, 2)
        self.assertEqual(self.cal.latest_result("C1").periodic_due, date(2026, 2, 11))

    def test_periodic_boundary_follows_same_inclusive_convention(self):
        self.cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        self.assertEqual(self.cal.overdue_kinds("C1", date(2026, 2, 4)), ())
        self.assertEqual(self.cal.overdue_kinds("C1", date(2026, 2, 5)), (PERIODIC,))

    def test_periodic_filing_due_today_clears_overdue_next_day(self):
        self.cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        self.cal.file_report("C1", PERIODIC, date(2026, 2, 4))  # on the deadline
        self.assertEqual(self.cal.overdue_kinds("C1", date(2026, 2, 10)), ())

    def test_filing_before_anchor_window_does_not_count(self):
        self.cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        self.cal.file_report("C1", PERIODIC, date(2026, 1, 4))
        self.assertEqual(self.cal.overdue_kinds("C1", date(2026, 2, 10)), (PERIODIC,))


class BlockedReviewTests(unittest.TestCase):
    def setUp(self):
        self.cal = calendar()
        self.cal.register_case(RegCase("C1", "major", date(2026, 1, 1), "monthly"))

    def test_under_review_result_blocks_case_and_views(self):
        self.cal.record_evidence(Evidence("L1", "C1", revision=1))  # accepted_at=None
        self.assertTrue(self.cal.pending_evidence("C1"))
        self.assertEqual(self.cal.status("C1", date(2026, 1, 15)), BLOCKED)
        entry = self.cal.entry("C1", date(2026, 1, 15))
        self.assertEqual(entry.blocked_reasons, ("evidence:L1:under_review",))

        views = self.cal.views(date(2026, 1, 15))
        self.assertEqual([e.case_id for e in views[BLOCKED]], ["C1"])
        self.assertEqual(views[DUE_SOON], [])

        ok, reasons = self.cal.can_close("C1", date(2026, 1, 15))
        self.assertFalse(ok)
        self.assertIn("evidence_under_review", reasons)
        with self.assertRaises(ValueError):
            self.cal.close_case("C1", date(2026, 1, 15))

    def test_block_lifts_after_review_accepted(self):
        self.cal.record_evidence(Evidence("L1", "C1", revision=1))
        self.cal.complete_review("C1", "L1", date(2026, 1, 8))
        self.assertEqual(self.cal.pending_evidence("C1"), ())
        self.assertNotEqual(self.cal.status("C1", date(2026, 1, 15)), BLOCKED)
        self.assertEqual(self.cal.latest_result("C1").anchor_evidence, "L1")


class CloseGateTests(unittest.TestCase):
    def test_overdue_mandatory_report_blocks_close(self):
        cal = calendar()
        cal.register_case(RegCase("C1", "critical", date(2026, 1, 1), "monthly"))
        ok, reasons = cal.can_close("C1", date(2026, 1, 12))
        self.assertFalse(ok)
        self.assertEqual(reasons, ("mandatory_report_overdue",))
        with self.assertRaises(ValueError):
            cal.close_case("C1", date(2026, 1, 12))

    def test_filed_mandatory_report_allows_close(self):
        cal = calendar()
        cal.register_case(RegCase("C1", "critical", date(2026, 1, 1), "monthly"))
        cal.file_report("C1", MANDATORY, date(2026, 1, 11))  # filed exactly on deadline
        self.assertTrue(cal.can_close("C1", date(2026, 1, 12))[0])
        closed = cal.close_case("C1", date(2026, 1, 12))
        self.assertEqual(closed.closed_at, date(2026, 1, 12))
        self.assertEqual(cal.status("C1", date(2026, 1, 12)), CLOSED)
        # Closed cases drop out of action views.
        views = cal.views(date(2026, 1, 12))
        self.assertEqual(sum(len(v) for v in views.values()), 0)

    def test_periodic_overdue_alone_does_not_block_close(self):
        cal = calendar()
        cal.register_case(RegCase("C1", "major", date(2026, 1, 1), "monthly"))
        cal.record_evidence(Evidence("E1", "C1", revision=1, accepted_at=date(2026, 1, 5)))
        cal.file_report("C1", MANDATORY, date(2026, 1, 20))
        # Periodic window (due 2026-02-04) is overdue but mandatory is satisfied.
        self.assertEqual(cal.overdue_kinds("C1", date(2026, 2, 10)), (PERIODIC,))
        self.assertEqual(cal.status("C1", date(2026, 2, 10)), OVERDUE)
        self.assertTrue(cal.can_close("C1", date(2026, 2, 10))[0])

    def test_overdue_and_blocked_both_reported(self):
        cal = calendar()
        cal.register_case(RegCase("C1", "critical", date(2026, 1, 1), "monthly"))
        cal.record_evidence(Evidence("L1", "C1", revision=1))
        ok, reasons = cal.can_close("C1", date(2026, 1, 12))
        self.assertFalse(ok)
        self.assertEqual(set(reasons), {"mandatory_report_overdue", "evidence_under_review"})


class ViewTests(unittest.TestCase):
    def test_views_bucket_cases(self):
        cal = calendar(warn_days=7)
        cal.register_case(RegCase("upcoming", "critical", date(2026, 1, 5), "monthly"))  # due Jan 15
        cal.register_case(RegCase("late", "critical", date(2026, 1, 1), "monthly"))  # overdue
        cal.register_case(RegCase("review", "major", date(2026, 1, 2), "monthly"))
        cal.record_evidence(Evidence("L1", "review", revision=1))

        views = cal.views(date(2026, 1, 12))
        ids = {k: [e.case_id for e in v] for k, v in views.items()}
        self.assertEqual(ids[OVERDUE], ["late"])
        self.assertEqual(ids[BLOCKED], ["review"])
        self.assertEqual(ids[DUE_SOON], ["upcoming"])


if __name__ == "__main__":
    unittest.main()
