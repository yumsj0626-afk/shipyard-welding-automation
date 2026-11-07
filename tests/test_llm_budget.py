"""Reservations survive failed requests, restarts, and competing connections."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from shipyard.llm_budget import APPLICATION_ID, BudgetExceeded, BudgetLedger, SCHEMA_VERSION


def open_ledger(path, project="project-A", *, report=100, run=200, total=300):
    return BudgetLedger(path, project, report, run, total)


@pytest.fixture
def ledger(tmp_path):
    value = open_ledger(tmp_path / "budget.sqlite3")
    yield value
    value.close()


def test_reservation_and_actual_settlement_release_only_unused_headroom(ledger):
    assert ledger.summary() == {
        "reserved_micro_usd": 0, "settled_micro_usd": 0,
        "committed_micro_usd": 0, "request_count": 0,
    }
    first = ledger.reserve("run-1", "report-1", 70)
    second = ledger.reserve("run-1", "report-1", 30)
    assert first != second
    assert ledger.summary("run-1")["committed_micro_usd"] == 100
    with pytest.raises(BudgetExceeded, match="REPORT_BUDGET_EXCEEDED"):
        ledger.reserve("run-1", "report-1", 1)
    ledger.settle(first, 20)
    assert ledger.summary() == {
        "reserved_micro_usd": 30, "settled_micro_usd": 20,
        "committed_micro_usd": 50, "request_count": 2,
    }
    ledger.reserve("run-1", "report-1", 50)
    assert ledger.summary()["committed_micro_usd"] == 100
    assert ledger.summary("missing-run")["request_count"] == 0


@pytest.mark.parametrize("scope,first_run,next_run,first_report,next_report,limits,code", [
    ("report", "r1", "r1", "p1", "p1", (10, 20, 30), "REPORT_BUDGET_EXCEEDED"),
    ("run", "r1", "r1", "p1", "p2", (20, 10, 30), "RUN_BUDGET_EXCEEDED"),
    ("project", "r1", "r2", "p1", "p1", (20, 30, 10), "PROJECT_BUDGET_EXCEEDED"),
])
def test_each_limit_is_checked_before_insert(tmp_path, scope, first_run, next_run, first_report, next_report, limits, code):
    value = BudgetLedger(tmp_path / f"{scope}.sqlite3", "project", *limits)
    try:
        value.reserve(first_run, first_report, 10)
        before = value.summary()
        with pytest.raises(BudgetExceeded) as error:
            value.reserve(next_run, next_report, 1)
        assert error.value.code == code
        assert value.summary() == before
    finally:
        value.close()


def test_failed_or_crashed_requests_remain_reserved_after_reopening(tmp_path):
    path = tmp_path / "budget.sqlite3"
    first = open_ledger(path)
    first.reserve("run-1", "report-1", 100)
    first.reserve("run-2", "report-1", 100)
    first.reserve("run-3", "report-1", 100)
    before = first.summary()
    first.close()  # No settlement: timeout, failure, or lost response is conservative.
    resumed = open_ledger(path)
    try:
        assert resumed.summary() == before
        assert resumed.summary()["reserved_micro_usd"] == 300
        with pytest.raises(BudgetExceeded) as error:
            resumed.reserve("new-session-run", "new-report", 1)
        assert error.value.code == "PROJECT_BUDGET_EXCEEDED"
        assert resumed.summary()["request_count"] == 3
    finally:
        resumed.close()


def test_settlement_is_idempotent_and_conflicting_or_excess_actual_cost_preserves_reservation(ledger):
    reservation = ledger.reserve("run", "report", 80)
    before = ledger.summary()
    with pytest.raises(ValueError, match="exceeds reservation"):
        ledger.settle(reservation, 81)
    assert ledger.summary() == before
    ledger.settle(reservation, 25)
    settled = ledger.summary()
    ledger.settle(reservation, 25)
    assert ledger.summary() == settled
    with pytest.raises(ValueError, match="different actual cost"):
        ledger.settle(reservation, 24)
    assert ledger.summary() == settled
    with pytest.raises(ValueError, match="unknown reservation"):
        ledger.settle("reservation:missing", 1)
    assert ledger.summary() == settled


def test_failed_settlement_write_rolls_back_without_releasing_reservation(ledger):
    reservation = ledger.reserve("run", "report", 80)
    before = ledger.summary()
    ledger._conn.execute("""
        CREATE TRIGGER fail_settlement BEFORE UPDATE ON budget_reservations
        BEGIN SELECT RAISE(ABORT, 'injected settlement write failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="injected settlement"):
        ledger.settle(reservation, 20)
    assert ledger.summary() == before
    ledger._conn.execute("DROP TRIGGER fail_settlement")
    ledger.settle(reservation, 20)
    assert ledger.summary() == {
        "reserved_micro_usd": 0, "settled_micro_usd": 20,
        "committed_micro_usd": 20, "request_count": 1,
    }


def test_different_projects_and_runs_are_independent_in_shared_ledger(tmp_path):
    path = tmp_path / "budget.sqlite3"
    first, other = open_ledger(path), open_ledger(path, "project-B")
    try:
        reservation = first.reserve("run", "report", 100)
        first.reserve("next-run", "report", 100)
        other.reserve("run", "report", 100)
        assert first.summary()["committed_micro_usd"] == 200
        assert first.summary("run")["committed_micro_usd"] == 100
        assert other.summary()["committed_micro_usd"] == 100
        with pytest.raises(ValueError, match="unknown reservation"):
            other.settle(reservation, 10)
        assert first.summary()["reserved_micro_usd"] == 200
    finally:
        first.close()
        other.close()


def test_concurrent_connections_cannot_both_claim_last_project_headroom(tmp_path):
    path = tmp_path / "budget.sqlite3"
    barrier = Barrier(2)

    def attempt(number):
        value = open_ledger(path, report=100, run=100, total=100)
        try:
            barrier.wait(timeout=5)
            try:
                return value.reserve(f"run-{number}", "report", 75)
            except BudgetExceeded as error:
                return error.code
        finally:
            value.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, range(2)))
    assert sum(item.startswith("reservation:") for item in results) == 1
    assert results.count("PROJECT_BUDGET_EXCEEDED") == 1
    resumed = open_ledger(path, report=100, run=100, total=100)
    try:
        assert resumed.summary() == {
            "reserved_micro_usd": 75, "settled_micro_usd": 0,
            "committed_micro_usd": 75, "request_count": 1,
        }
    finally:
        resumed.close()


def test_project_limits_cannot_be_silently_raised_on_restart(tmp_path):
    path = tmp_path / "budget.sqlite3"
    value = open_ledger(path)
    value.reserve("run", "report", 100)
    value.close()
    with pytest.raises(ValueError, match="persisted ledger limits"):
        open_ledger(path, total=400)
    resumed = open_ledger(path)
    try:
        assert resumed.summary()["committed_micro_usd"] == 100
    finally:
        resumed.close()


def test_request_count_survives_settlement_and_process_restart(tmp_path):
    path = tmp_path / "budget.sqlite3"
    original = open_ledger(path)
    first = original.reserve("run", "report", 10, max_requests_per_report=2)
    original.settle(first, 1)
    original.reserve("run", "report", 10, max_requests_per_report=2)
    before = original.summary()
    original.close()  # The second request was sent, but no result was committed.
    resumed = open_ledger(path)
    try:
        with pytest.raises(BudgetExceeded) as error:
            resumed.reserve("run", "report", 1, max_requests_per_report=2)
        assert error.value.code == "REPORT_REQUEST_LIMIT_EXCEEDED"
        assert resumed.summary() == before
        assert before["request_count"] == 2 and before["committed_micro_usd"] == 11
    finally:
        resumed.close()


def test_competing_connections_share_the_last_request_slot(tmp_path):
    path = tmp_path / "budget.sqlite3"
    original = open_ledger(path)
    original.reserve("run", "report", 1, max_requests_per_report=2)
    original.close()
    barrier = Barrier(2)

    def attempt(_):
        value = open_ledger(path)
        try:
            barrier.wait(timeout=5)
            try:
                return value.reserve("run", "report", 1, max_requests_per_report=2)
            except BudgetExceeded as error:
                return error.code
        finally:
            value.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, range(2)))
    assert sum(item.startswith("reservation:") for item in results) == 1
    assert results.count("REPORT_REQUEST_LIMIT_EXCEEDED") == 1
    resumed = open_ledger(path)
    try:
        assert resumed.summary()["request_count"] == 2
        assert resumed.summary()["reserved_micro_usd"] == 2
    finally:
        resumed.close()


def test_request_limit_is_scoped_to_project_run_and_report(tmp_path):
    path = tmp_path / "budget.sqlite3"
    first, other = open_ledger(path), open_ledger(path, "project-B")
    try:
        first.reserve("run-1", "report-1", 1, max_requests_per_report=1)
        first.reserve("run-2", "report-1", 1, max_requests_per_report=1)
        first.reserve("run-1", "report-2", 1, max_requests_per_report=1)
        other.reserve("run-1", "report-1", 1, max_requests_per_report=1)
        assert first.summary("run-1")["request_count"] == 2
        assert first.summary("run-2")["request_count"] == 1
        assert other.summary()["request_count"] == 1
        with pytest.raises(BudgetExceeded, match="REPORT_REQUEST_LIMIT_EXCEEDED"):
            first.reserve("run-1", "report-1", 1, max_requests_per_report=1)
    finally:
        first.close()
        other.close()


def test_none_request_limit_preserves_existing_budget_only_behavior(ledger):
    for _ in range(3):
        ledger.reserve("run", "report", 1, max_requests_per_report=None)
    assert ledger.summary()["request_count"] == 3


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.0, "2"])
def test_request_limit_requires_a_positive_integer_and_rejects_bool(ledger, limit):
    with pytest.raises(ValueError, match="max_requests_per_report"):
        ledger.reserve("run", "report", 1, max_requests_per_report=limit)
    assert ledger.summary()["request_count"] == 0


@pytest.mark.parametrize("amount", [0, -1, True, False, 1.0, "1", None, 1 << 63])
def test_amounts_must_be_positive_integers_without_bool_or_overflow(tmp_path, ledger, amount):
    before = ledger.summary()
    with pytest.raises(ValueError):
        ledger.reserve("run", "report", amount)
    reservation = ledger.reserve("run", "report", 1)
    with pytest.raises(ValueError):
        ledger.settle(reservation, amount)
    assert ledger.summary()["reserved_micro_usd"] == 1
    assert before["request_count"] == 0
    for field in range(3):
        limits = [10, 20, 30]
        limits[field] = amount
        path = tmp_path / f"invalid-{field}.sqlite3"
        with pytest.raises(ValueError):
            BudgetLedger(path, "project", *limits)
        assert not path.exists()


@pytest.mark.parametrize("identifier", ["", " ", "bad\x00id", None, 1])
def test_invalid_scope_ids_cannot_create_untracked_reservations(ledger, identifier):
    for run_id, report_id in ((identifier, "report"), ("run", identifier)):
        with pytest.raises(ValueError):
            ledger.reserve(run_id, report_id, 1)
    assert ledger.summary()["request_count"] == 0


def test_existing_unrelated_database_or_file_is_not_modified(tmp_path):
    database = tmp_path / "existing.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE valuable_data(value TEXT)")
    connection.execute("INSERT INTO valuable_data VALUES ('keep me')")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()
    before = database.read_bytes()
    with pytest.raises(ValueError, match="supported LLM budget ledger"):
        open_ledger(database)
    assert database.read_bytes() == before
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("original user data")
    before = unrelated.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        open_ledger(unrelated)
    assert unrelated.read_bytes() == before


def test_ledger_records_explicit_schema_identity_and_rejects_future_schema(tmp_path):
    path = tmp_path / "budget.sqlite3"
    value = open_ledger(path)
    value.close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    connection.execute("PRAGMA user_version=999")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="supported LLM budget ledger"):
        open_ledger(path)
    assert path.read_bytes() == before
