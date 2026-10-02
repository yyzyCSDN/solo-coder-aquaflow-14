"""Regulatory case and periodic-filing deadline calendar.

Deadlines are derived from three inputs:

* case severity            -- mandatory reporting window (calendar days)
* reporting cycle          -- periodic filing window (calendar days)
* latest *accepted* evidence -- anchor for the next periodic filing

Every recomputation appends a new immutable :class:`DeadlineResult` to an
append-only ledger; prior results are never edited, so an evidence revision
is always auditable against the calculation it replaced.  Evidence that is
still under review neither anchors a deadline nor supersedes accepted
evidence, but it *blocks* the case until the review completes.

A case cannot be closed while its mandatory report is overdue.

Boundary convention: a deadline falling exactly on ``at`` is "due today"
(it is still upcoming).  It becomes overdue only once ``at`` is later than
the deadline date.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

MANDATORY = "mandatory"
PERIODIC = "periodic"

DUE_SOON = "due_soon"
OVERDUE = "overdue"
BLOCKED = "blocked"
SCHEDULED = "scheduled"
CLOSED = "closed"


@dataclass(frozen=True)
class SeverityRule:
    """Mandatory reporting window for a case severity level."""

    severity: str
    mandatory_days: int


@dataclass(frozen=True)
class Cycle:
    """A recurring reporting cycle."""

    name: str
    period_days: int


@dataclass(frozen=True)
class RegCase:
    case_id: str
    severity: str
    opened_at: date
    cycle: str
    closed_at: date | None = None

    @property
    def closed(self) -> bool:
        return self.closed_at is not None


@dataclass(frozen=True)
class Evidence:
    """A piece of evidence, possibly still under review.

    ``accepted_at`` is ``None`` while the result is under review.  A revision
    names the older revision it replaces via ``supersedes``.
    """

    evidence_id: str
    case_id: str
    revision: int
    accepted_at: date | None = None
    supersedes: int | None = None

    @property
    def under_review(self) -> bool:
        return self.accepted_at is None


@dataclass(frozen=True)
class DeadlineResult:
    """Immutable snapshot of the deadline calculation for one case."""

    result_id: int
    case_id: str
    mandatory_due: date
    periodic_due: date | None
    anchor_evidence: str | None
    anchor_revision: int | None
    basis: str
    computed_at: date
    supersedes_result: int | None = None
    trigger: str = ""


@dataclass(frozen=True)
class Filing:
    case_id: str
    kind: str
    on: date


@dataclass(frozen=True)
class CalendarEntry:
    case_id: str
    status: str
    mandatory_due: date
    periodic_due: date | None
    overdue_kinds: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()


class DeadlineCalendar:
    """Compute regulatory deadlines and expose action views.

    State changes that move a deadline (case registration, accepted evidence,
    accepted review) append a fresh :class:`DeadlineResult`; the result ledger
    is append-only and old results are never mutated.
    """

    def __init__(
        self,
        severity_rules: list[SeverityRule],
        cycles: list[Cycle],
        warn_days: int = 7,
    ):
        self._rules = {r.severity: r for r in severity_rules}
        self._cycles = {c.name: c for c in cycles}
        self.warn_days = warn_days
        self._cases: dict[str, RegCase] = {}
        self._evidence: dict[str, list[Evidence]] = {}
        self._filings: dict[str, list[Filing]] = {}
        self._results: dict[str, list[DeadlineResult]] = {}
        self._seq = 0

    # ------------------------------------------------------------------ registration

    def register_case(self, case: RegCase) -> DeadlineResult:
        if case.case_id in self._cases:
            raise ValueError("case already registered")
        if case.severity not in self._rules:
            raise ValueError("unknown severity")
        if case.cycle not in self._cycles:
            raise ValueError("unknown cycle")
        self._cases[case.case_id] = case
        self._evidence[case.case_id] = []
        self._filings[case.case_id] = []
        self._results[case.case_id] = []
        return self._recompute(case.case_id, case.opened_at, "registered", None)

    # ------------------------------------------------------------------ evidence

    def record_evidence(self, ev: Evidence) -> DeadlineResult | None:
        """Record evidence.

        Accepted evidence anchors a fresh periodic deadline and yields a new
        result.  Evidence still under review is held (no recomputation) and
        blocks the case until :meth:`complete_review`.
        """
        self._require_case(ev.case_id)
        if any(e.evidence_id == ev.evidence_id for e in self._evidence[ev.case_id]):
            raise ValueError("evidence already recorded")
        self._evidence[ev.case_id].append(ev)
        if ev.under_review:
            return None
        return self._recompute(ev.case_id, ev.accepted_at, "evidence", ev)

    def complete_review(self, case_id: str, evidence_id: str, accepted_at: date) -> DeadlineResult:
        """Accept previously submitted evidence and recompute deadlines."""
        self._require_case(case_id)
        rows = self._evidence[case_id]
        for i, ev in enumerate(rows):
            if ev.evidence_id == evidence_id:
                if not ev.under_review:
                    raise ValueError("evidence already reviewed")
                rows[i] = replace(ev, accepted_at=accepted_at)
                return self._recompute(case_id, accepted_at, "review", rows[i])
        raise ValueError("unknown evidence")

    def pending_evidence(self, case_id: str) -> tuple[Evidence, ...]:
        return tuple(e for e in self._evidence.get(case_id, ()) if e.under_review)

    def accepted_evidence(self, case_id: str) -> tuple[Evidence, ...]:
        return tuple(e for e in self._evidence.get(case_id, ()) if not e.under_review)

    def current_evidence(self, case_id: str) -> Evidence | None:
        """Latest accepted evidence not covered by a newer revision.

        Superseded revisions (including those replaced by evidence that is
        itself still under review) are never the anchor.
        """
        accepted = self.accepted_evidence(case_id)
        # Only accepted revisions cover their predecessor: a revision still
        # under review must leave the old evidence in force.
        replaced = {e.supersedes for e in accepted if e.supersedes is not None}
        active = [e for e in accepted if e.revision not in replaced]
        if not active:
            return None
        return max(active, key=lambda e: (e.revision, e.accepted_at))

    # ------------------------------------------------------------------ filings

    def file_report(self, case_id: str, kind: str, on: date) -> None:
        self._require_case(case_id)
        if kind not in (MANDATORY, PERIODIC):
            raise ValueError("unknown filing kind")
        self._filings[case_id].append(Filing(case_id, kind, on))

    def is_mandatory_filed(self, case_id: str) -> bool:
        return any(f.kind == MANDATORY for f in self._filings[case_id])

    def is_periodic_filed(self, case_id: str, at: date) -> bool:
        result = self.latest_result(case_id)
        if result is None or result.periodic_due is None or result.anchor_evidence is None:
            return False
        anchor = self._anchor_accepted_at(case_id, result.anchor_revision)
        return any(
            f.kind == PERIODIC and anchor is not None and anchor <= f.on <= result.periodic_due
            for f in self._filings[case_id]
        )

    # ------------------------------------------------------------------ ledger

    def results(self, case_id: str) -> tuple[DeadlineResult, ...]:
        """All calculation results, oldest first. Prior entries never change."""
        return tuple(self._results.get(case_id, ()))

    def latest_result(self, case_id: str) -> DeadlineResult | None:
        rows = self._results.get(case_id, ())
        return rows[-1] if rows else None

    def _recompute(
        self,
        case_id: str,
        computed_at: date,
        trigger: str,
        ev: Evidence | None,
    ) -> DeadlineResult:
        case = self._cases[case_id]
        mandatory_due = case.opened_at + timedelta(
            days=self._rules[case.severity].mandatory_days
        )
        anchor = self.current_evidence(case_id)
        if anchor is not None:
            periodic_due: date | None = anchor.accepted_at + timedelta(
                days=self._cycles[case.cycle].period_days
            )
            basis = f"evidence:{anchor.evidence_id}"
        else:
            periodic_due = None
            basis = "registered"
        prior = self.latest_result(case_id)
        self._seq += 1
        result = DeadlineResult(
            result_id=self._seq,
            case_id=case_id,
            mandatory_due=mandatory_due,
            periodic_due=periodic_due,
            anchor_evidence=anchor.evidence_id if anchor else None,
            anchor_revision=anchor.revision if anchor else None,
            basis=basis,
            computed_at=computed_at,
            supersedes_result=prior.result_id if prior else None,
            trigger=trigger,
        )
        self._results[case_id].append(result)
        return result

    # ------------------------------------------------------------------ status / views

    def overdue_kinds(self, case_id: str, at: date) -> tuple[str, ...]:
        result = self.latest_result(case_id)
        if result is None:
            return ()
        kinds = []
        if not self.is_mandatory_filed(case_id) and at > result.mandatory_due:
            kinds.append(MANDATORY)
        if not self.is_periodic_filed(case_id, at) and result.periodic_due is not None and at > result.periodic_due:
            kinds.append(PERIODIC)
        return tuple(kinds)

    def status(self, case_id: str, at: date) -> str:
        case = self._cases[case_id]
        if case.closed:
            return CLOSED
        if self.pending_evidence(case_id):
            return BLOCKED
        if self.overdue_kinds(case_id, at):
            return OVERDUE
        result = self.latest_result(case_id)
        horizon = at + timedelta(days=self.warn_days)
        outstanding = [
            d
            for d, filed in (
                (result.mandatory_due, self.is_mandatory_filed(case_id)),
                (result.periodic_due, self.is_periodic_filed(case_id, at)),
            )
            if d is not None and not filed
        ]
        if any(at <= d <= horizon for d in outstanding):
            return DUE_SOON
        return SCHEDULED

    def entry(self, case_id: str, at: date) -> CalendarEntry:
        result = self.latest_result(case_id)
        pending = self.pending_evidence(case_id)
        return CalendarEntry(
            case_id=case_id,
            status=self.status(case_id, at),
            mandatory_due=result.mandatory_due,
            periodic_due=result.periodic_due,
            overdue_kinds=self.overdue_kinds(case_id, at),
            blocked_reasons=tuple(f"evidence:{e.evidence_id}:under_review" for e in pending),
        )

    def views(self, at: date) -> dict[str, list[CalendarEntry]]:
        """Upcoming / overdue / blocked views across open cases."""
        buckets: dict[str, list[CalendarEntry]] = {DUE_SOON: [], OVERDUE: [], BLOCKED: []}
        for case_id, case in self._cases.items():
            if case.closed:
                continue
            entry = self.entry(case_id, at)
            if entry.status in buckets:
                buckets[entry.status].append(entry)
        for rows in buckets.values():
            rows.sort(key=lambda e: (e.periodic_due or e.mandatory_due, e.case_id))
        return buckets

    # ------------------------------------------------------------------ closing

    def can_close(self, case_id: str, at: date) -> tuple[bool, tuple[str, ...]]:
        """A case cannot close while blocked or with an overdue mandatory report."""
        self._require_case(case_id)
        reasons = []
        if self.pending_evidence(case_id):
            reasons.append("evidence_under_review")
        if MANDATORY in self.overdue_kinds(case_id, at):
            reasons.append("mandatory_report_overdue")
        return (not reasons, tuple(reasons))

    def close_case(self, case_id: str, at: date) -> RegCase:
        ok, reasons = self.can_close(case_id, at)
        if not ok:
            raise ValueError("case cannot be closed: " + ",".join(reasons))
        case = self._cases[case_id]
        closed = replace(case, closed_at=at)
        self._cases[case_id] = closed
        return closed

    # ------------------------------------------------------------------ internals

    def _anchor_accepted_at(self, case_id: str, revision: int) -> date | None:
        for e in self._evidence[case_id]:
            if e.revision == revision and not e.under_review:
                return e.accepted_at
        return None

    def _require_case(self, case_id: str) -> None:
        if case_id not in self._cases:
            raise ValueError("unknown case")
