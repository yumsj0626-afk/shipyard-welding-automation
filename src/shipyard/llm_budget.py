"""Persistent request reservations, separate from the simulation state database.

A request is charged its full reservation until a successful settlement records
its actual cost. Closing the process, a transport failure, or a timeout does not
release that reservation. Amounts are integer millionths of a US dollar.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4


SCHEMA_VERSION = 1
APPLICATION_ID = 0x53594247  # SYBG: distinguish this ledger from other SQLite files.
MAX_SQLITE_INTEGER = (1 << 63) - 1


class BudgetExceeded(RuntimeError):
    """A reservation would exceed a persisted report, run, or project limit."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _positive_amount(value: int, name: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_SQLITE_INTEGER:
        raise ValueError(f"{name} must be a positive SQLite integer in micro USD")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty string without NUL")
    return value


class BudgetLedger:
    """One project view of a shared SQLite ledger with atomic budget checks.

    Report limits apply within a run; project totals span all runs and process
    restarts. A project's limits are immutable in this minimal schema, so opening
    another connection cannot silently raise its previously configured budget.
    """

    def __init__(self, path: str | Path, project_id: str,
                 report_limit_micro_usd: int, run_limit_micro_usd: int,
                 project_limit_micro_usd: int):
        self.project_id = _identifier(project_id, "project_id")
        limits = tuple(_positive_amount(value, name) for value, name in (
            (report_limit_micro_usd, "report_limit_micro_usd"),
            (run_limit_micro_usd, "run_limit_micro_usd"),
            (project_limit_micro_usd, "project_limit_micro_usd"),
        ))
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            with self._transaction():
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
                application = self._conn.execute("PRAGMA application_id").fetchone()[0]
                objects = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if version == 0 and application == 0 and not objects:
                    self._create_schema()
                elif version != SCHEMA_VERSION or application != APPLICATION_ID:
                    raise ValueError("not a supported LLM budget ledger; choose a new ledger path")
                stored = self._conn.execute(
                    "SELECT report_limit_micro_usd,run_limit_micro_usd,project_limit_micro_usd "
                    "FROM budget_projects WHERE project_id=?", (self.project_id,),
                ).fetchone()
                if stored is None:
                    self._conn.execute(
                        "INSERT INTO budget_projects VALUES (?,?,?,?)", (self.project_id, *limits),
                    )
                elif tuple(stored) != limits:
                    raise ValueError("project budget limits differ from the persisted ledger limits")
        except BaseException:
            self._conn.close()
            raise

    @contextmanager
    def _transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _create_schema(self):
        self._conn.execute("""
            CREATE TABLE budget_projects (
                project_id TEXT PRIMARY KEY,
                report_limit_micro_usd INTEGER NOT NULL CHECK(report_limit_micro_usd > 0),
                run_limit_micro_usd INTEGER NOT NULL CHECK(run_limit_micro_usd > 0),
                project_limit_micro_usd INTEGER NOT NULL CHECK(project_limit_micro_usd > 0)
            )
        """)
        self._conn.execute("""
            CREATE TABLE budget_reservations (
                reservation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES budget_projects(project_id),
                run_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                reserved_micro_usd INTEGER NOT NULL
                    CHECK(typeof(reserved_micro_usd) = 'integer' AND reserved_micro_usd > 0),
                actual_micro_usd INTEGER
                    CHECK(actual_micro_usd IS NULL OR (
                        typeof(actual_micro_usd) = 'integer' AND actual_micro_usd > 0
                        AND actual_micro_usd <= reserved_micro_usd))
            )
        """)
        self._conn.execute(
            "CREATE INDEX budget_scope ON budget_reservations(project_id,run_id,report_id)"
        )
        self._conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def reserve(self, run_id: str, report_id: str, amount_micro_usd: int, *,
                max_requests_per_report: int | None = None) -> str:
        """Reserve cost and, when requested, a persistent per-report request slot.

        Settled reservations still count as requests. An abandoned reservation
        also retains its slot, including across process restarts.
        """
        run_id = _identifier(run_id, "run_id")
        report_id = _identifier(report_id, "report_id")
        amount = _positive_amount(amount_micro_usd, "amount_micro_usd")
        if max_requests_per_report is not None and (
            type(max_requests_per_report) is not int or max_requests_per_report <= 0
        ):
            raise ValueError("max_requests_per_report must be a positive integer or None")
        with self._transaction():
            report_limit, run_limit, project_limit = self._conn.execute(
                "SELECT report_limit_micro_usd,run_limit_micro_usd,project_limit_micro_usd "
                "FROM budget_projects WHERE project_id=?", (self.project_id,),
            ).fetchone()
            totals = self._conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN run_id=? AND report_id=?
                        THEN COALESCE(actual_micro_usd,reserved_micro_usd) ELSE 0 END),0),
                    COALESCE(SUM(CASE WHEN run_id=?
                        THEN COALESCE(actual_micro_usd,reserved_micro_usd) ELSE 0 END),0),
                    COALESCE(SUM(COALESCE(actual_micro_usd,reserved_micro_usd)),0),
                    COUNT(CASE WHEN run_id=? AND report_id=? THEN 1 END)
                FROM budget_reservations WHERE project_id=?
            """, (run_id, report_id, run_id, run_id, report_id, self.project_id)).fetchone()
            if max_requests_per_report is not None and totals[3] >= max_requests_per_report:
                raise BudgetExceeded("REPORT_REQUEST_LIMIT_EXCEEDED")
            for total, limit, code in zip(totals[:3], (report_limit, run_limit, project_limit), (
                "REPORT_BUDGET_EXCEEDED", "RUN_BUDGET_EXCEEDED", "PROJECT_BUDGET_EXCEEDED",
            )):
                if amount > limit - total:
                    raise BudgetExceeded(code)
            reservation_id = "reservation:" + uuid4().hex
            self._conn.execute(
                "INSERT INTO budget_reservations VALUES (?,?,?,?,?,NULL)",
                (reservation_id, self.project_id, run_id, report_id, amount),
            )
        return reservation_id

    def settle(self, reservation_id: str, actual_micro_usd: int) -> None:
        reservation_id = _identifier(reservation_id, "reservation_id")
        actual = _positive_amount(actual_micro_usd, "actual_micro_usd")
        with self._transaction():
            row = self._conn.execute(
                "SELECT reserved_micro_usd,actual_micro_usd FROM budget_reservations "
                "WHERE reservation_id=? AND project_id=?", (reservation_id, self.project_id),
            ).fetchone()
            if row is None:
                raise ValueError("unknown reservation for this project")
            reserved, previous = row
            if actual > reserved:
                raise ValueError("actual cost exceeds reservation; reservation remains committed")
            if previous is not None:
                if actual != previous:
                    raise ValueError("reservation is already settled at a different actual cost")
                return
            self._conn.execute(
                "UPDATE budget_reservations SET actual_micro_usd=? WHERE reservation_id=? AND project_id=?",
                (actual, reservation_id, self.project_id),
            )

    def summary(self, run_id: str | None = None) -> dict:
        if run_id is not None:
            run_id = _identifier(run_id, "run_id")
        where, parameters = "project_id=?", [self.project_id]
        if run_id is not None:
            where += " AND run_id=?"
            parameters.append(run_id)
        reserved, settled, count = self._conn.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN actual_micro_usd IS NULL
                        THEN reserved_micro_usd ELSE 0 END),0),
                   COALESCE(SUM(actual_micro_usd),0),COUNT(*)
            FROM budget_reservations WHERE {where}
        """, parameters).fetchone()
        return {
            "reserved_micro_usd": reserved,
            "settled_micro_usd": settled,
            "committed_micro_usd": reserved + settled,
            "request_count": count,
        }

    def close(self) -> None:
        self._conn.close()
