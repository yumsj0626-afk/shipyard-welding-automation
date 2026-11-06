"""The single SQLite writer for public state and its audit trail."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import (
    BaselineAssignment, BaselinePlan, CheckResult, Delivery, Instruction, Observation, Plan, PlanningSnapshot,
    canonical, digest, utc_now,
)


class StaleStateError(RuntimeError):
    """The caller's state is no longer the database state."""


class IdentityConflictError(ValueError):
    """An immutable ID was reused with different contents."""


def delivery_fingerprint(delivery: Delivery) -> str:
    event = delivery.event.model_dump(mode="json", exclude={"received_tick"})
    for key in ("evidence_ids", "restriction_ids", "unknown_fields"):
        event[key] = sorted(event[key])
    evidence = []
    for item in sorted(delivery.published_evidence, key=lambda e: e.evidence_id):
        row = item.model_dump(mode="json")
        row["related_evidence_ids"] = sorted(row["related_evidence_ids"])
        evidence.append(row)
    return digest({"event": event, "published_evidence": evidence})


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        tables = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version == 0 and not tables:
            self._conn.executescript(Path(__file__).with_name("schema.sql").read_text())
        elif version != 1:
            self._conn.close()
            raise ValueError("unsupported database schema; use a new run directory")

    def close(self):
        self._conn.close()

    def initialize(self, snapshot: PlanningSnapshot) -> PlanningSnapshot:
        if snapshot.state_version != 0 or snapshot.active_plan_version != 0:
            raise ValueError("initial versions must be zero")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self._conn.execute("SELECT 1 FROM runs").fetchone():
                raise ValueError("run database is already initialized")
            self._conn.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (
                snapshot.run_id, 0, snapshot.now_tick, 0, canonical(snapshot),
            ))
            self._sync_entities(snapshot)
            self._audit(snapshot, "INITIALIZE", "APPLIED", {}, 0, 0)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.read_snapshot()

    def read_snapshot(self) -> PlanningSnapshot:
        row = self._conn.execute("SELECT snapshot_json FROM runs").fetchone()
        if row is None:
            raise ValueError("run database is not initialized")
        return PlanningSnapshot.model_validate_json(row[0])

    snapshot = read_snapshot

    def _current(self, expected_version: int) -> PlanningSnapshot:
        current = self.read_snapshot()
        if current.state_version != expected_version:
            raise StaleStateError("STALE_PLAN: state version changed")
        return current

    def _sync_entities(self, snapshot: PlanningSnapshot):
        if len({e.evidence_id for e in snapshot.evidence}) != len(snapshot.evidence):
            raise IdentityConflictError("duplicate evidence IDs in snapshot")
        if len({i.instruction_id for i in snapshot.current_instructions}) != len(snapshot.current_instructions):
            raise IdentityConflictError("duplicate instruction IDs in snapshot")
        sources = {s.source_id: s for s in snapshot.sources}
        for item in snapshot.evidence:
            source = sources.get(item.source_id)
            if (item.available_tick > snapshot.now_tick or not source
                    or item.observation_kind not in source.allowed_observation_kinds
                    or item.target.scope not in source.allowed_target_scopes):
                raise ValueError("evidence is not currently visible or authorized")
            payload = canonical(item)
            row = self._conn.execute(
                "SELECT payload_hash FROM evidence WHERE run_id=? AND evidence_id=?",
                (snapshot.run_id, item.evidence_id),
            ).fetchone()
            if row and row[0] != digest(item):
                raise IdentityConflictError(f"evidence ID conflict: {item.evidence_id}")
            self._conn.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)", (
                snapshot.run_id, item.evidence_id, digest(item), payload,
            ))
        for item in snapshot.current_instructions:
            old = self._conn.execute(
                "SELECT payload_json FROM instructions WHERE run_id=? AND instruction_id=?",
                (snapshot.run_id, item.instruction_id),
            ).fetchone()
            if old:
                previous = json.loads(old[0])
                for key in ("plan_id", "job_id", "robot_id", "action", "not_before_tick", "expected_end_tick"):
                    if previous[key] != getattr(item, key):
                        raise IdentityConflictError(f"instruction ID conflict: {item.instruction_id}")
            self._conn.execute(
                "INSERT INTO instructions VALUES (?,?,?,?,?) ON CONFLICT(run_id,instruction_id) "
                "DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json",
                (snapshot.run_id, item.instruction_id, item.plan_id, item.status, canonical(item)),
            )

    def _store(self, proposed: PlanningSnapshot, current: PlanningSnapshot) -> PlanningSnapshot:
        if proposed.run_id != current.run_id or proposed.now_tick < current.now_tick:
            raise ValueError("run identity and simulation time cannot go backwards")
        value = proposed.model_copy(deep=True)
        changed = (
            value.model_dump(mode="json", exclude={"state_version"})
            != current.model_dump(mode="json", exclude={"state_version"})
        )
        value.state_version = current.state_version + int(changed)
        value = PlanningSnapshot.model_validate(value.model_dump())
        self._sync_entities(value)
        updated = self._conn.execute(
            "UPDATE runs SET state_version=?,now_tick=?,active_plan_version=?,snapshot_json=? "
            "WHERE run_id=? AND state_version=? AND now_tick=? AND active_plan_version=?",
            (value.state_version, value.now_tick, value.active_plan_version, canonical(value),
             current.run_id, current.state_version, current.now_tick, current.active_plan_version),
        )
        if updated.rowcount != 1:
            raise StaleStateError("STALE_PLAN: compare-and-swap failed")
        return value

    def _audit(self, snapshot, stage, outcome, data, before, after):
        record = {
            "run_id": snapshot.run_id, "tick": snapshot.now_tick,
            "received_at": utc_now(), "stage": stage, "actor": "controller",
            "outcome": outcome, "state_version_before": before,
            "state_version_after": after, "data": data,
        }
        self._conn.execute(
            "INSERT INTO audit_log(run_id,tick,stage,outcome,payload_json) VALUES (?,?,?,?,?)",
            (snapshot.run_id, snapshot.now_tick, stage, outcome, canonical(record)),
        )

    def audit(self, stage, outcome, data, *, state_version_before=None, state_version_after=None):
        current = self.read_snapshot()
        self._audit(
            current, stage, outcome, data,
            current.state_version if state_version_before is None else state_version_before,
            current.state_version if state_version_after is None else state_version_after,
        )

    def audit_records(self) -> list[dict]:
        return [dict(json.loads(row["payload_json"]), seq=row["log_seq"])
                for row in self._conn.execute("SELECT log_seq,payload_json FROM audit_log ORDER BY log_seq")]

    def commit_snapshot(self, new_snapshot, expected_version, stage, outcome="APPLIED", audit_data=None):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._current(expected_version)
            if new_snapshot.active_plan_version != current.active_plan_version:
                raise ValueError("only apply_plan changes the active plan version")
            stored = self._store(new_snapshot, current)
            self._audit(stored, stage, outcome, audit_data or {}, current.state_version, stored.state_version)
            self._conn.execute("COMMIT")
            return stored
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def filter_deliveries(self, deliveries: list[Delivery]) -> tuple[list[Delivery], list[dict]]:
        """Read-only deduplication; commit_event_batch checks the keys again."""
        current = self.read_snapshot()
        grouped: dict[str, list[Delivery]] = {}
        for delivery in deliveries:
            grouped.setdefault(delivery.event.event_id, []).append(delivery)
        new, results = [], []
        for event_id, group in grouped.items():
            fingerprints = {delivery_fingerprint(d) for d in group}
            row = self._conn.execute(
                "SELECT fingerprint,result_json,state_version FROM events WHERE run_id=? AND event_id=?",
                (current.run_id, event_id),
            ).fetchone()
            if len(fingerprints) > 1 or (row and row[0] not in fingerprints):
                results.append({"event_id": event_id, "outcome": "ID_CONFLICT",
                                "reason_codes": ["EVENT_ID_REUSED"], "intervention_kind": "ADDITIONAL_HUMAN_REQUEST"})
            elif row:
                result = json.loads(row[1])
                results.append({**result, "outcome": "DUPLICATE", "original_outcome": result["outcome"],
                                "original_state_version": row[2]})
            else:
                new.append(group[0])
        return new, results

    def commit_event_batch(self, new_snapshot, deliveries, results, expected_version):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._current(expected_version)
            if new_snapshot.active_plan_version != current.active_plan_version:
                raise ValueError("events cannot activate plans")
            result_by_id = {r["event_id"]: r for r in results}
            if set(result_by_id) != {d.event.event_id for d in deliveries}:
                raise ValueError("every delivered event needs exactly one processing result")
            for delivery in deliveries:
                row = self._conn.execute(
                    "SELECT fingerprint FROM events WHERE run_id=? AND event_id=?",
                    (current.run_id, delivery.event.event_id),
                ).fetchone()
                result = result_by_id[delivery.event.event_id]
                if row and result["outcome"] not in {"DUPLICATE", "ID_CONFLICT"}:
                    raise StaleStateError("event was handled after the caller's read")
                if row and result["outcome"] == "DUPLICATE" and row[0] != delivery_fingerprint(delivery):
                    raise IdentityConflictError("event fingerprint changed")
            stored = self._store(new_snapshot, current)
            logged = set()
            for delivery in deliveries:
                event = delivery.event
                result = result_by_id[event.event_id]
                fingerprint = delivery_fingerprint(delivery)
                self._conn.execute(
                    "INSERT INTO event_receipts(run_id,event_id,fingerprint,outcome,tick,payload_json) VALUES (?,?,?,?,?,?)",
                    (current.run_id, event.event_id, fingerprint, result["outcome"], stored.now_tick, canonical(delivery)),
                )
                if event.event_id in logged:
                    continue
                logged.add(event.event_id)
                if result["outcome"] != "DUPLICATE":
                    self._conn.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (
                        current.run_id, event.event_id, fingerprint, canonical(delivery), canonical(result), stored.state_version,
                    ))
                if result["outcome"] != "DUPLICATE" and result.get("lookup_passes"):
                    self._audit(stored, "QUERY", "FOUND" if result["lookup_evidence_ids"] else "MISSING", {
                        "event_id": event.event_id, "query_kind": "PUBLIC_SNAPSHOT",
                        "lookup_passes": result["lookup_passes"], "evidence_ids": result["lookup_evidence_ids"],
                    }, current.state_version, stored.state_version)
                self._audit(stored, "EVENT", result["outcome"],
                            {"event": event.model_dump(mode="json"), "result": result,
                             "published_evidence": [e.model_dump(mode="json") for e in delivery.published_evidence]},
                            current.state_version, stored.state_version)
                for restriction in ([] if result["outcome"] == "DUPLICATE" else result.get("restrictions", [])):
                    self._audit(stored, "RESTRICTION", restriction["status"], {"restriction": restriction},
                                current.state_version, stored.state_version)
                if result.get("intervention_kind") == "ADDITIONAL_HUMAN_REQUEST" and result["outcome"] != "DUPLICATE":
                    self._audit(stored, "HUMAN_REQUEST", "REQUESTED",
                                {"event_id": event.event_id, "reason_codes": result.get("reason_codes", [])},
                                current.state_version, stored.state_version)
            self._conn.execute("COMMIT")
            return stored
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _record_plan(self, snapshot: PlanningSnapshot, plan: Plan, check: CheckResult | None):
        old = self._conn.execute("SELECT payload_json FROM plans WHERE run_id=? AND plan_id=?",
                                 (snapshot.run_id, plan.plan_id)).fetchone()
        if old and old[0] != canonical(plan):
            raise IdentityConflictError("plan ID conflict")
        if not old:
            self._conn.execute("INSERT INTO plans VALUES (?,?,?,?)", (
                snapshot.run_id, plan.plan_id, "CANDIDATE", canonical(plan),
            ))
            self._audit(snapshot, "PLAN", plan.solver_result.status, {"plan": plan.model_dump(mode="json")},
                        snapshot.state_version, snapshot.state_version)
        if check:
            old_check = self._conn.execute("SELECT payload_json FROM checks WHERE run_id=? AND check_id=?",
                                           (snapshot.run_id, check.check_id)).fetchone()
            if old_check and old_check[0] != canonical(check):
                raise IdentityConflictError("check ID conflict")
            if not old_check:
                self._conn.execute("INSERT INTO checks VALUES (?,?,?,?,?)", (
                    snapshot.run_id, check.check_id, plan.plan_id, int(check.passed), canonical(check),
                ))
                self._audit(snapshot, "CHECK", "PASSED" if check.passed else "REJECTED",
                            {"check": check.model_dump(mode="json")}, snapshot.state_version, snapshot.state_version)

    def record_plan(self, plan: Plan, check: CheckResult | None = None):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._record_plan(self.read_snapshot(), plan, check)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def apply_plan(self, plan: Plan, check: CheckResult, instructions: list[Instruction]):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._current(plan.based_on_state_version)
            expected = (current.now_tick, current.active_plan_version, current.policy_version)
            if (plan.based_on_tick, plan.based_on_active_plan_version, plan.policy_version) != expected:
                raise StaleStateError("STALE_PLAN: tick, plan or policy changed")
            if (check.checked_state_version, check.checked_tick, check.checked_active_plan_version) != (
                current.state_version, current.now_tick, current.active_plan_version,
            ):
                raise StaleStateError("STALE_PLAN: check snapshot changed")
            if not check.passed or check.plan_id != plan.plan_id or check.plan_payload_hash != plan.payload_hash:
                raise ValueError("CHECK_REJECTED: missing matching successful check")
            if plan.run_id != current.run_id or check.policy_version != current.policy_version:
                raise ValueError("plan/check run or policy mismatch")
            if digest(plan.model_dump(mode="json", exclude={"payload_hash"})) != plan.payload_hash:
                raise ValueError("plan payload changed after checking")
            if plan.solver_result.status not in {"OPTIMAL", "FEASIBLE_NOT_PROVEN", "NO_ELIGIBLE_WORK"}:
                raise ValueError("solver result is not applicable")
            ids = [i.instruction_id for i in instructions]
            old_ids = {i.instruction_id for i in current.current_instructions}
            if (len(ids) != len(set(ids)) or old_ids.intersection(ids)
                    or any(i.plan_id != plan.plan_id for i in instructions)):
                raise ValueError("invalid new instruction identities")
            self._record_plan(current, plan, check)
            proposed = current.model_copy(deep=True)
            for instruction in proposed.current_instructions:
                if instruction.status == "PENDING":
                    instruction.status = "CANCELLED"
                    instruction.superseded_by = plan.plan_id
            for instruction in instructions:
                item = instruction.model_copy(deep=True)
                item.accepted_state_version = current.state_version + 1
                item.accepted_plan_version = current.active_plan_version + 1
                proposed.current_instructions.append(item)
            proposed.baseline_plan = BaselinePlan(
                plan_id=plan.plan_id,
                assignments=[BaselineAssignment(job_id=a.job_id, robot_id=a.robot_id, weld_start=a.weld_start)
                             for a in plan.assignments],
            )
            proposed.active_plan_version += 1
            stored = self._store(proposed, current)
            self._conn.execute("UPDATE plans SET status='APPLIED' WHERE run_id=? AND plan_id=?",
                               (current.run_id, plan.plan_id))
            self._audit(stored, "APPLY", "APPLIED", {
                "plan_id": plan.plan_id, "instructions": [i.model_dump(mode="json") for i in stored.current_instructions
                                                          if i.plan_id == plan.plan_id],
            }, current.state_version, stored.state_version)
            self._conn.execute("COMMIT")
            return stored
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def observation_result(self, observation: Observation) -> str | None:
        current = self.read_snapshot()
        row = self._conn.execute("SELECT payload_hash FROM observations WHERE run_id=? AND observation_id=?",
                                 (current.run_id, observation.observation_id)).fetchone()
        if row:
            if row[0] != digest(observation):
                raise IdentityConflictError("observation ID conflict")
            return "DUPLICATE"
        return None

    def observation_receipt(self, observation: Observation) -> bool:
        return self.observation_result(observation) == "DUPLICATE"

    def commit_observation(self, new_snapshot, observation: Observation, expected_version):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self.observation_result(observation) == "DUPLICATE":
                self._conn.execute("COMMIT")
                return self.read_snapshot()
            current = self._current(expected_version)
            if new_snapshot.active_plan_version != current.active_plan_version:
                raise ValueError("observations cannot activate plans")
            stored = self._store(new_snapshot, current)
            self._conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", (
                stored.run_id, observation.observation_id, observation.execution_id,
                observation.instruction_id, digest(observation), canonical(observation),
            ))
            stage = "PHASE_START" if observation.kind == "PHASE_STARTED" else "PHASE_END"
            self._audit(stored, stage, "APPLIED", {"observation": observation.model_dump(mode="json")},
                        current.state_version, stored.state_version)
            self._conn.execute("COMMIT")
            return stored
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
