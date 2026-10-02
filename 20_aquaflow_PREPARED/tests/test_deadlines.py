import dataclasses
import unittest
from datetime import datetime, timedelta, timezone

from aquaflow.regulatory import cases
from aquaflow.regulatory.deadlines import (
    Evidence,
    Obligation,
    blocked,
    calculate,
    calendar,
    close_case,
    closeable,
    current,
    current_all,
    due_soon,
    latest_accepted,
    overdue,
    recalculate,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
MICRO = timedelta(microseconds=1)


def obligation(**kw):
    base = dict(obligation_id="ob-1", case_id="case-1", severity=1, cycle="once")
    base.update(kw)
    return Obligation(**base)


def accepted(evidence_id, at, revision=1, supersedes=None, case_id="case-1"):
    return Evidence(evidence_id, case_id, revision, "accepted", at, accepted_at=at, supersedes=supersedes)


class ComputationTest(unittest.TestCase):
    def test_once_deadline_uses_severity_window(self):
        calc = calculate(obligation(severity=2), [], T0, T0)
        self.assertEqual(calc.anchor, T0)
        self.assertEqual(calc.deadline, T0 + timedelta(hours=72))
        self.assertIsNone(calc.evidence_id)
        self.assertEqual(calc.version, 1)

    def test_periodic_deadline_uses_cycle(self):
        calc = calculate(obligation(cycle="weekly"), [], T0, T0)
        self.assertEqual(calc.deadline, T0 + timedelta(weeks=1))
        quarterly = calculate(obligation(cycle="quarterly"), [], T0, T0)
        self.assertEqual(quarterly.deadline, T0 + timedelta(days=91))

    def test_latest_accepted_evidence_anchors_deadline(self):
        accepted_at = T0 + 2 * HOUR
        rows = [accepted("ev-1", accepted_at)]
        calc = calculate(obligation(), rows, T0, T0 + 3 * HOUR)
        self.assertEqual(calc.anchor, accepted_at)
        self.assertEqual(calc.evidence_id, "ev-1")
        self.assertEqual(calc.deadline, accepted_at + timedelta(hours=24))

    def test_invalid_severity_and_cycle_rejected(self):
        with self.assertRaises(ValueError):
            calculate(obligation(severity=0), [], T0, T0)
        with self.assertRaises(ValueError):
            calculate(obligation(severity=5), [], T0, T0)
        with self.assertRaises(ValueError):
            calculate(obligation(cycle="yearly"), [], T0, T0)


class DeadlineDayBoundaryTest(unittest.TestCase):
    """A report is still on time on the deadline day itself."""

    def setUp(self):
        self.calc = calculate(obligation(), [], T0, T0)  # deadline T0 + 24h
        self.deadline = self.calc.deadline

    def test_exactly_at_deadline_is_not_overdue(self):
        self.assertEqual(overdue([self.calc], self.deadline), [])

    def test_exactly_at_deadline_is_still_due_soon(self):
        self.assertEqual(due_soon([self.calc], self.deadline, timedelta(0)), [self.calc])

    def test_one_moment_before_deadline_is_on_time(self):
        self.assertEqual(overdue([self.calc], self.deadline - MICRO), [])
        self.assertEqual(due_soon([self.calc], self.deadline - MICRO, MICRO), [self.calc])

    def test_one_moment_after_deadline_is_overdue(self):
        now = self.deadline + MICRO
        self.assertEqual(overdue([self.calc], now), [self.calc])
        self.assertEqual(due_soon([self.calc], now, DAY), [])

    def test_due_soon_horizon_boundary_is_inclusive(self):
        now = T0
        horizon = self.deadline - now
        self.assertEqual(due_soon([self.calc], now, horizon), [self.calc])
        self.assertEqual(due_soon([self.calc], now, horizon - MICRO), [])

    def test_negative_horizon_rejected(self):
        with self.assertRaises(ValueError):
            due_soon([self.calc], T0, -MICRO)


class EvidenceRevisionTest(unittest.TestCase):
    """Revisions append new calculations; old results are never edited."""

    def setUp(self):
        self.t1 = T0 + HOUR
        self.ev1 = accepted("ev-1", self.t1, revision=1)
        self.ob = obligation()
        self.calc1 = calculate(self.ob, [self.ev1], T0, self.t1)

    def test_revision_produces_new_calculation_and_keeps_old_one(self):
        t2 = T0 + 5 * HOUR
        ev2 = accepted("ev-2", t2, revision=2, supersedes="ev-1")
        calc2 = recalculate(self.ob, [self.ev1, ev2], T0, t2, [self.calc1])

        self.assertEqual(calc2.version, 2)
        self.assertEqual(calc2.anchor, t2)
        self.assertEqual(calc2.evidence_id, "ev-2")
        self.assertEqual(calc2.deadline, t2 + timedelta(hours=24))

        history = [self.calc1, calc2]
        self.assertIs(current(history, "ob-1"), calc2)
        self.assertEqual(current_all(history), [calc2])
        # The old result still stands with its original deadline.
        self.assertEqual(self.calc1.version, 1)
        self.assertEqual(self.calc1.anchor, self.t1)
        self.assertEqual(self.calc1.deadline, self.t1 + timedelta(hours=24))

    def test_calculation_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.calc1.deadline = T0

    def test_superseded_evidence_no_longer_anchors(self):
        t2 = T0 + 5 * HOUR
        ev2 = accepted("ev-2", t2, revision=2, supersedes="ev-1")
        latest = latest_accepted([self.ev1, ev2], "case-1")
        self.assertEqual(latest.evidence_id, "ev-2")

    def test_recalculate_rejects_time_reversal(self):
        with self.assertRaises(ValueError):
            recalculate(self.ob, [self.ev1], T0, self.t1 - MICRO, [self.calc1])


class UnderReviewBlockingTest(unittest.TestCase):
    """Lab results still in review block the case until accepted."""

    def setUp(self):
        self.t1 = T0 + HOUR
        self.ev1 = accepted("ev-1", self.t1, revision=1)
        self.review = Evidence("ev-2", "case-1", 2, "under_review", T0 + 2 * HOUR, supersedes="ev-1")
        self.ob = obligation()

    def test_case_is_blocked_while_revision_under_review(self):
        self.assertEqual(blocked([self.ev1, self.review]), ["case-1"])

    def test_under_review_revision_does_not_move_deadline(self):
        calc = calculate(self.ob, [self.ev1, self.review], T0, T0 + 3 * HOUR)
        self.assertEqual(calc.evidence_id, "ev-1")
        self.assertEqual(calc.anchor, self.t1)

    def test_acceptance_unblocks_and_reanchors(self):
        rows = [self.ev1, self.review]
        self.assertEqual(blocked(rows), ["case-1"])
        accepted_ev2 = accepted("ev-2", T0 + 4 * HOUR, revision=2, supersedes="ev-1")
        rows = [self.ev1, accepted_ev2]
        self.assertEqual(blocked(rows), [])
        calc = recalculate(self.ob, rows, T0, T0 + 4 * HOUR, [])
        self.assertEqual(calc.evidence_id, "ev-2")
        self.assertEqual(calc.anchor, T0 + 4 * HOUR)


class CloseGuardTest(unittest.TestCase):
    """A case cannot close while a mandatory report is overdue."""

    def setUp(self):
        record = cases.CasesRecord("case-1", "open")
        events = [("investigate", T0), ("freeze_evidence", T0), ("report", T0), ("follow_up", T0)]
        self.record = cases.replay(record, events)
        self.ob = obligation()

    def test_overdue_mandatory_report_blocks_close(self):
        calc = calculate(self.ob, [], T0, T0)  # deadline T0 + 24h
        now = calc.deadline + MICRO
        self.assertFalse(closeable("case-1", [self.ob], [calc], now))
        with self.assertRaises(ValueError):
            close_case(self.record, [self.ob], [calc], now)
        self.assertEqual(self.record.state, "followup")

    def test_close_allowed_on_deadline_day(self):
        calc = calculate(self.ob, [], T0, T0)
        closed = close_case(self.record, [self.ob], [calc], calc.deadline)
        self.assertEqual(closed.state, "closed")

    def test_new_evidence_recalculation_allows_close(self):
        calc1 = calculate(self.ob, [], T0, T0)
        now = calc1.deadline + MICRO
        with self.assertRaises(ValueError):
            close_case(self.record, [self.ob], [calc1], now)
        ev = accepted("ev-1", now)
        calc2 = recalculate(self.ob, [ev], T0, now, [calc1])
        history = [calc1, calc2]
        self.assertTrue(closeable("case-1", [self.ob], history, now))
        closed = close_case(self.record, [self.ob], history, now)
        self.assertEqual(closed.state, "closed")

    def test_overdue_optional_report_does_not_block_close(self):
        optional = obligation(obligation_id="ob-2", mandatory=False)
        calc = calculate(optional, [], T0, T0)
        now = calc.deadline + MICRO
        closed = close_case(self.record, [optional], [calc], now)
        self.assertEqual(closed.state, "closed")

    def test_other_cases_obligations_do_not_block_close(self):
        other = obligation(obligation_id="ob-9", case_id="case-9")
        calc = calculate(other, [], T0, T0)
        now = calc.deadline + MICRO
        self.assertTrue(closeable("case-1", [other], [calc], now))


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.ob1 = obligation(obligation_id="ob-1", severity=1)
        self.ob2 = obligation(obligation_id="ob-2", case_id="case-2", severity=3)
        self.calc1 = calculate(self.ob1, [], T0, T0)  # T0 + 24h
        self.calc2 = calculate(self.ob2, [], T0, T0)  # T0 + 7d

    def test_views_split_by_deadline(self):
        history = [self.calc1, self.calc2]
        now = T0 + 2 * DAY
        self.assertEqual(overdue(history, now), [self.calc1])
        self.assertEqual(due_soon(history, now, 6 * DAY), [self.calc2])

    def test_views_use_latest_calculation_only(self):
        ev = accepted("ev-1", T0 + 2 * DAY)
        calc1_v2 = recalculate(self.ob1, [ev], T0, T0 + 2 * DAY, [self.calc1])
        history = [self.calc1, calc1_v2, self.calc2]
        now = T0 + 2 * DAY
        self.assertEqual(overdue(history, now), [])
        self.assertEqual(due_soon(history, now, 6 * DAY), [calc1_v2, self.calc2])

    def test_views_sorted_by_deadline(self):
        history = [self.calc2, self.calc1]
        self.assertEqual(due_soon(history, T0, 30 * DAY), [self.calc1, self.calc2])

    def test_blocked_lists_distinct_cases_sorted(self):
        rows = [
            Evidence("e1", "case-b", 1, "under_review", T0),
            Evidence("e2", "case-a", 1, "under_review", T0),
            Evidence("e3", "case-b", 2, "under_review", T0),
            Evidence("e4", "case-c", 1, "accepted", T0, accepted_at=T0),
        ]
        self.assertEqual(blocked(rows), ["case-a", "case-b"])

    def test_calendar_combines_all_views(self):
        review = Evidence("e9", "case-3", 1, "under_review", T0)
        view = calendar([self.calc1, self.calc2], [review], T0 + 2 * DAY, 6 * DAY)
        self.assertEqual(view["overdue"], [self.calc1])
        self.assertEqual(view["due_soon"], [self.calc2])
        self.assertEqual(view["blocked"], ["case-3"])


if __name__ == "__main__":
    unittest.main()
