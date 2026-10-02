"""Regulatory case deadline calendar.

Deadlines derive from case severity, reporting cycle, and the latest
accepted evidence.  Evidence revisions append new calculations instead of
mutating earlier ones, so the calendar keeps a full audit trail.  A case
cannot close while one of its mandatory reports is overdue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .cases import CasesRecord, apply as apply_transition

# Severity 1 is the most severe.  One-off case reports are due within the
# severity window; periodic reports recur every fixed-length cycle.
SEVERITY_WINDOWS = {
    1: timedelta(hours=24),
    2: timedelta(hours=72),
    3: timedelta(days=7),
    4: timedelta(days=30),
}

CYCLE_PERIODS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=91),
}

EVIDENCE_STATES = ("under_review", "accepted", "rejected")


@dataclass(frozen=True)
class Obligation:
    """One reporting duty attached to a regulatory case."""
    obligation_id: str
    case_id: str
    severity: int
    cycle: str  # "once" or a CYCLE_PERIODS key
    mandatory: bool = True


@dataclass(frozen=True)
class Evidence:
    """One evidence revision; acceptance anchors deadline computation."""
    evidence_id: str
    case_id: str
    revision: int
    state: str  # one of EVIDENCE_STATES
    submitted_at: datetime
    accepted_at: datetime | None = None
    supersedes: str | None = None


@dataclass(frozen=True)
class Calculation:
    """Immutable deadline computation for one obligation."""
    obligation_id: str
    case_id: str
    version: int
    anchor: datetime
    evidence_id: str | None
    deadline: datetime
    computed_at: datetime


def _window(obligation: Obligation) -> timedelta:
    if obligation.severity not in SEVERITY_WINDOWS:
        raise ValueError("severity outside 1..4")
    if obligation.cycle == "once":
        return SEVERITY_WINDOWS[obligation.severity]
    if obligation.cycle not in CYCLE_PERIODS:
        raise ValueError("unknown cycle")
    return CYCLE_PERIODS[obligation.cycle]


def latest_accepted(rows: list[Evidence], case_id: str) -> Evidence | None:
    """Newest accepted revision; a revision takes effect once accepted."""
    accepted = [e for e in rows if e.case_id == case_id and e.state == "accepted"]
    superseded = {e.supersedes for e in accepted if e.supersedes is not None}
    active = [e for e in accepted if e.evidence_id not in superseded]
    if not active:
        return None
    return max(active, key=lambda e: (e.revision, e.evidence_id))


def _compute(obligation: Obligation, rows: list[Evidence], opened: datetime, computed_at: datetime, version: int) -> Calculation:
    window = _window(obligation)
    evidence = latest_accepted(rows, obligation.case_id)
    anchor = evidence.accepted_at if evidence is not None else opened
    return Calculation(
        obligation_id=obligation.obligation_id,
        case_id=obligation.case_id,
        version=version,
        anchor=anchor,
        evidence_id=evidence.evidence_id if evidence is not None else None,
        deadline=anchor + window,
        computed_at=computed_at,
    )


def calculate(obligation: Obligation, rows: list[Evidence], opened: datetime, computed_at: datetime) -> Calculation:
    """First computation for an obligation (version 1)."""
    return _compute(obligation, rows, opened, computed_at, 1)


def recalculate(obligation: Obligation, rows: list[Evidence], opened: datetime, computed_at: datetime, history: list[Calculation]) -> Calculation:
    """Append-only recomputation; prior calculations stay untouched."""
    previous = current(history, obligation.obligation_id)
    if previous is not None and computed_at < previous.computed_at:
        raise ValueError("time reversal")
    version = 1 if previous is None else previous.version + 1
    return _compute(obligation, rows, opened, computed_at, version)


def current(calculations: list[Calculation], obligation_id: str) -> Calculation | None:
    rows = [c for c in calculations if c.obligation_id == obligation_id]
    if not rows:
        return None
    return max(rows, key=lambda c: c.version)


def current_all(calculations: list[Calculation]) -> list[Calculation]:
    """Latest calculation per obligation, ordered by obligation id."""
    latest: dict[str, Calculation] = {}
    for calc in calculations:
        prior = latest.get(calc.obligation_id)
        if prior is None or prior.version < calc.version:
            latest[calc.obligation_id] = calc
    return [latest[key] for key in sorted(latest)]


def due_soon(calculations: list[Calculation], now: datetime, horizon: timedelta) -> list[Calculation]:
    """Current calculations due within [now, now + horizon]."""
    if horizon < timedelta(0):
        raise ValueError("negative horizon")
    rows = [c for c in current_all(calculations) if now <= c.deadline <= now + horizon]
    return sorted(rows, key=lambda c: (c.deadline, c.obligation_id))


def overdue(calculations: list[Calculation], now: datetime) -> list[Calculation]:
    """Current calculations whose deadline is strictly before now."""
    rows = [c for c in current_all(calculations) if c.deadline < now]
    return sorted(rows, key=lambda c: (c.deadline, c.obligation_id))


def blocked(rows: list[Evidence]) -> list[str]:
    """Cases with evidence still under review."""
    return sorted({e.case_id for e in rows if e.state == "under_review"})


def calendar(calculations: list[Calculation], rows: list[Evidence], now: datetime, horizon: timedelta) -> dict[str, list]:
    """Combined due-soon / overdue / blocked view."""
    return {
        "due_soon": due_soon(calculations, now, horizon),
        "overdue": overdue(calculations, now),
        "blocked": blocked(rows),
    }


def closeable(case_id: str, obligations: list[Obligation], calculations: list[Calculation], now: datetime) -> bool:
    """False while any mandatory report for the case is overdue."""
    latest = {c.obligation_id: c for c in current_all(calculations)}
    for obligation in obligations:
        if obligation.case_id != case_id or not obligation.mandatory:
            continue
        calc = latest.get(obligation.obligation_id)
        if calc is not None and calc.deadline < now:
            return False
    return True


def close_case(record: CasesRecord, obligations: list[Obligation], calculations: list[Calculation], at: datetime) -> CasesRecord:
    """Close a case only when no mandatory report is overdue."""
    if not closeable(record.record_id, obligations, calculations, at):
        raise ValueError("mandatory report overdue")
    return apply_transition(record, "close", at)
