"""Shared report-outcome store that later runs search for similar past results.

Each finished run appends one row per report: the text, the event the agent
classified it as, the cited rules, and what actually happened afterwards
(restrictions opened/released, hold length, whether the target job finished).
Retrieval only returns rows from runs that finished before the caller's cutoff,
so a run can never see its own or a later run's outcomes.
"""
from __future__ import annotations

import json
import math
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Protocol

MAX_REPORT_CHARS = 4000
DEFAULT_TOP_K = 3
MAX_TOP_K = 5
EMBED_BATCH = 32
SCHEMA = """
CREATE TABLE IF NOT EXISTS report_outcomes (
    outcome_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    data_policy TEXT NOT NULL,
    run_started_at TEXT NOT NULL,
    run_finished_at TEXT NOT NULL,
    run_termination TEXT NOT NULL,
    report_id TEXT NOT NULL,
    incident_id TEXT,
    observed_tick INTEGER,
    received_tick INTEGER,
    text TEXT NOT NULL,
    target_json TEXT NOT NULL,
    adapter_status TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    event_kind TEXT,
    event_outcome TEXT,
    evidence_ids_json TEXT NOT NULL,
    rules_retrieved_json TEXT NOT NULL,
    rule_refs_json TEXT NOT NULL,
    restrictions_json TEXT NOT NULL,
    job_id TEXT,
    job_completed INTEGER CHECK (job_completed IN (0, 1)),
    job_weld_end_tick INTEGER,
    UNIQUE (run_id, report_id)
);
CREATE INDEX IF NOT EXISTS report_outcomes_finished ON report_outcomes(run_finished_at);
CREATE TABLE IF NOT EXISTS outcome_embeddings (
    outcome_id TEXT NOT NULL REFERENCES report_outcomes(outcome_id),
    model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    PRIMARY KEY (outcome_id, model)
);
"""


def normalized_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def unit_vector(vector: list[float]) -> tuple[float, ...]:
    if not vector or not all(type(value) in (float, int) and math.isfinite(value) for value in vector):
        raise ValueError("Invalid embedding vector")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid embedding norm")
    return tuple(value / norm for value in vector)


class EmbeddingClient(Protocol):
    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...


def outcomes_from_trace(trace: list[dict], snapshot: dict) -> list[dict]:
    """Join each report's adapter result with the events, restrictions and
    completions that followed it in the same run."""
    events = {}
    restrictions = {}
    for row in trace:
        data = row["data"]
        if row["stage"] == "EVENT" and "event" in data:
            events.setdefault(data["event"]["event_id"], (row["outcome"], data["event"]))
        elif row["stage"] == "RESTRICTION":
            restriction = data["restriction"]
            restrictions[restriction["restriction_id"]] = restriction
    jobs = {job["job_id"]: job for job in snapshot["jobs"]}
    outcomes = []
    for row in trace:
        if row["stage"] != "REPORT_RESULT":
            continue
        result = row["data"]
        report = result["received_report"]["report"]
        event_outcome, event = events.get(report["report_id"], (None, None))
        delivery_event = (result.get("delivery") or {}).get("event")
        event = event or delivery_event
        target = (event or {}).get("target") or report.get("target_hint") or {"scope": "UNRESOLVED"}
        retrieved, cited = [], []
        for step in result["trace"]:
            if step["step"] == "LOOKUP":
                retrieved = step.get("rule_refs", [])
            elif step["step"] == "RULE_CITATIONS":
                cited = step["rule_refs"]
        linked = []
        for restriction in restrictions.values():
            opened = restriction["opened_event_id"] == report["report_id"]
            released = restriction.get("released_event_id") == report["report_id"]
            if not (opened or released):
                continue
            end = restriction.get("released_tick")
            linked.append({
                "restriction_id": restriction["restriction_id"], "kind": restriction["kind"],
                "opened_by_this_report": opened, "released_by_this_report": released,
                "opened_tick": restriction["opened_tick"], "released_tick": end,
                "status_at_run_end": restriction["status"],
                "hold_ticks": end - restriction["opened_tick"] if end is not None else None,
            })
        job_id = target.get("job_id")
        job = jobs.get(job_id) if job_id else None
        outcomes.append({
            "report_id": report["report_id"], "incident_id": report.get("incident_id"),
            "observed_tick": report.get("observed_tick"), "received_tick": report.get("received_tick"),
            "text": report["text"], "target": target, "adapter_status": result["status"],
            "reason_codes": result["reason_codes"],
            "event_kind": (event or {}).get("kind"), "event_outcome": event_outcome,
            "evidence_ids": (event or {}).get("evidence_ids", []),
            "rules_retrieved": retrieved, "rule_refs": cited,
            "restrictions": sorted(linked, key=lambda item: item["restriction_id"]),
            "job_id": job_id,
            "job_completed": None if job is None else job["lifecycle"] == "COMPLETED",
            "job_weld_end_tick": None if job is None else job.get("actual_weld_end"),
        })
    return outcomes


class OutcomeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    def record_run(self, run_dir: Path, *, case_id: str, data_policy: str, started_at: str,
                   finished_at: str, termination: str) -> int:
        """Read a finished run's exported trace and its final snapshot; append outcomes."""
        run_dir = Path(run_dir)
        trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        uri = f"{(run_dir / 'run.sqlite3').resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            run_id, snapshot_json = db.execute("SELECT run_id, snapshot_json FROM runs").fetchone()
        outcomes = outcomes_from_trace(trace, json.loads(snapshot_json))
        return self.add_outcomes(outcomes, run_id=run_id, case_id=case_id, data_policy=data_policy,
                                 started_at=started_at, finished_at=finished_at, termination=termination)

    def add_outcomes(self, outcomes: list[dict], *, run_id: str, case_id: str, data_policy: str,
                     started_at: str, finished_at: str, termination: str) -> int:
        dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._conn:
            for item in outcomes:
                self._conn.execute(
                    "INSERT INTO report_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{run_id}:{item['report_id']}", run_id, case_id, data_policy, started_at, finished_at,
                     termination, item["report_id"], item["incident_id"], item["observed_tick"],
                     item["received_tick"], item["text"], dump(item["target"]), item["adapter_status"],
                     dump(item["reason_codes"]), item["event_kind"], item["event_outcome"],
                     dump(item["evidence_ids"]), dump(item["rules_retrieved"]), dump(item["rule_refs"]),
                     dump(item["restrictions"]), item["job_id"],
                     None if item["job_completed"] is None else int(item["job_completed"]),
                     item["job_weld_end_tick"]))
        return len(outcomes)

    def eligible(self, *, before: str, data_policy: str, exclude_run_id: str | None = None,
                 exclude_case_id: str | None = None) -> list[dict]:
        rows = self._conn.execute(
            "SELECT outcome_id, case_id, run_finished_at, run_termination, report_id, text, target_json, "
            "adapter_status, event_kind, event_outcome, rule_refs_json, restrictions_json, job_completed "
            "FROM report_outcomes WHERE run_finished_at < ? AND data_policy = ? "
            "AND run_id IS NOT ? AND case_id IS NOT ? ORDER BY run_finished_at DESC, outcome_id",
            (before, data_policy, exclude_run_id, exclude_case_id)).fetchall()
        result = []
        for row in rows:
            (outcome_id, case_id, finished, termination, report_id, text, target, status,
             kind, event_outcome, rule_refs, restrictions, completed) = row
            result.append({
                "id": outcome_id, "case_id": case_id, "finished_at": finished, "text": text,
                "outcome": {
                    "target": json.loads(target), "adapter_status": status, "event_kind": kind,
                    "event_outcome": event_outcome, "rule_refs": json.loads(rule_refs),
                    "restrictions": [{key: r[key] for key in ("kind", "opened_tick", "released_tick", "hold_ticks",
                                                              "status_at_run_end")}
                                     for r in json.loads(restrictions)],
                    "job_completed": None if completed is None else bool(completed),
                    "run_termination": termination,
                },
            })
        return result

    def vectors(self, model: str, outcome_ids: list[str]) -> dict[str, tuple[float, ...]]:
        found = {}
        for start in range(0, len(outcome_ids), 500):
            chunk = outcome_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for outcome_id, vector in self._conn.execute(
                    f"SELECT outcome_id, vector_json FROM outcome_embeddings WHERE model = ? AND outcome_id IN ({marks})",
                    (model, *chunk)):
                found[outcome_id] = unit_vector(json.loads(vector))
        return found

    def save_vectors(self, model: str, vectors: dict[str, tuple[float, ...]]):
        with self._conn:
            self._conn.executemany("INSERT OR REPLACE INTO outcome_embeddings VALUES (?,?,?)",
                                   [(key, model, json.dumps(list(value))) for key, value in vectors.items()])

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM report_outcomes").fetchone()[0]


class HistoryRetriever:
    """Embedding search over outcomes finished before `before`.

    Rows without a cached vector for the model are embedded on first use and
    cached, so outcomes recorded by offline (fake) runs become searchable later.
    """

    def __init__(self, store: OutcomeStore, client: EmbeddingClient, model: str, *, before: str,
                 data_policy: str, exclude_run_id: str | None = None, exclude_case_id: str | None = None,
                 top_k: int = DEFAULT_TOP_K):
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top-k must be between 1 and {MAX_TOP_K}")
        self.store, self.client, self.model, self.top_k = store, client, model, top_k
        self.before, self.data_policy = before, data_policy
        self.exclude_run_id, self.exclude_case_id = exclude_run_id, exclude_case_id
        self.candidates = store.eligible(before=before, data_policy=data_policy,
                                         exclude_run_id=exclude_run_id, exclude_case_id=exclude_case_id)

    def describe(self) -> dict:
        return {"store": self.store.path.name, "embedding_model": self.model, "top_k": self.top_k,
                "cutoff": self.before, "excluded_case_id": self.exclude_case_id,
                "eligible_outcomes": len(self.candidates)}

    def _vectors(self) -> dict[str, tuple[float, ...]]:
        ids = [row["id"] for row in self.candidates]
        cached = self.store.vectors(self.model, ids)
        missing = [row for row in self.candidates if row["id"] not in cached]
        fresh = {}
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start:start + EMBED_BATCH]
            vectors = self.client.embed(self.model, [row["text"] for row in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding client did not return one vector per outcome")
            fresh.update({row["id"]: unit_vector(vector) for row, vector in zip(batch, vectors, strict=True)})
        if fresh:
            self.store.save_vectors(self.model, fresh)
        return {**cached, **fresh}

    def search(self, text: str, report_id: str | None = None) -> list[dict]:
        if not normalized_text(text) or len(text) > MAX_REPORT_CHARS:
            raise ValueError(f"Query must contain 1 to {MAX_REPORT_CHARS} characters")
        if not self.candidates:
            return []
        query = unit_vector(self.client.embed(self.model, [text])[0])
        vectors = self._vectors()
        if {len(vector) for vector in vectors.values()} != {len(query)}:
            raise ValueError("Embedding dimensions differ from cached outcome vectors")
        ranked = sorted(((math.sumprod(query, vectors[row["id"]]), row) for row in self.candidates),
                        key=lambda item: (-item[0], item[1]["id"]))
        results, seen = [], set()
        for score, row in ranked:
            key = normalized_text(row["text"])
            if key in seen:
                continue
            seen.add(key)
            results.append({"id": row["id"], "score": round(score, 6), "text": row["text"],
                            "outcome": row["outcome"]})
            if len(results) == self.top_k:
                break
        return results
