"""One CP-SAT backend; public input only, no database or simulator access."""
from __future__ import annotations

import time

import ortools
from ortools.sat.python import cp_model

from .contracts import (
    Assignment, ConstraintSet, Plan, PlanningSnapshot, Policy, SolverResult,
    digest, new_id,
)


def _run_solver(model, policy, remaining_seconds):
    """Small seam for testing termination without timing-dependent tests."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = remaining_seconds
    solver.parameters.num_search_workers = policy.num_search_workers
    solver.parameters.random_seed = policy.random_seed
    return solver, solver.solve(model)


def solve(snapshot: PlanningSnapshot, constraints: ConstraintSet, policy: Policy) -> Plan:
    started = time.monotonic()
    stages = []
    assignments = []
    best_values = []
    proven = 0
    raw_status = "NOT_RUN"

    def finish(status, reason):
        return Plan(
            run_id=snapshot.run_id, plan_id=new_id("plan"),
            based_on_state_version=snapshot.state_version, based_on_tick=snapshot.now_tick,
            based_on_active_plan_version=snapshot.active_plan_version,
            policy_version=policy.policy_version, rule_version=policy.rule_version,
            constraint_hash=constraints.constraint_hash,
            required_job_ids=constraints.required_job_ids,
            deferred_jobs=constraints.deferred_jobs, fixed_segments=constraints.fixed_executions,
            assignments=assignments,
            solver_result=SolverResult(
                status=status, objective_values=best_values, proven_stages=proven,
                elapsed_ms=(time.monotonic() - started) * 1000,
                reason_codes=[reason], num_search_workers=policy.num_search_workers,
                random_seed=policy.random_seed, time_limit_seconds=policy.solver_seconds,
                raw_status=raw_status, stages=stages, solver_version=ortools.__version__,
                termination_reason=reason,
            ),
        ).seal()

    ref = constraints.snapshot_ref
    if (ref.run_id, ref.state_version, ref.active_plan_version, ref.now_tick) != (
        snapshot.run_id, snapshot.state_version, snapshot.active_plan_version, snapshot.now_tick
    ):
        return finish("MODEL_INVALID", "CONSTRAINT_SNAPSHOT_MISMATCH")
    if constraints.constraint_hash != digest(constraints.model_dump(mode="json", exclude={"constraint_hash"})):
        return finish("MODEL_INVALID", "CONSTRAINT_HASH_MISMATCH")

    jobs = {j.job_id: j for j in snapshot.jobs}
    robots = {r.robot_id: r for r in snapshot.robots}
    progress = {p.job_id: p for p in snapshot.execution_progress}
    travel = {(t.from_zone_id, t.to_zone_id): t.duration_ticks for t in constraints.travel_setup}
    fixed_welds = {f.job_id: f for f in constraints.fixed_executions if f.phase == "WELD"}
    pending = sorted(set(constraints.required_job_ids) - fixed_welds.keys())
    if not pending:
        return finish("NO_ELIGIBLE_WORK", "NO_NEW_WORK_CURRENT_FIXED_EXECUTIONS_CONTINUE")

    model = cp_model.CpModel()
    horizon, now = constraints.horizon_end, snapshot.now_tick
    robot_intervals = {r: [] for r in robots}
    zone_intervals = {z.zone_id: [] for z in snapshot.zones}
    available_at = {r: now for r in robots}
    initial_location = {r: obj.location_zone_id for r, obj in robots.items()}
    for fixed in constraints.fixed_executions:
        if fixed.end_tick < fixed.start_tick:
            return finish("MODEL_INVALID", "INVALID_FIXED_DURATION")
        interval = model.new_interval_var(fixed.start_tick, fixed.end_tick - fixed.start_tick,
            fixed.end_tick, f"fixed_{fixed.execution_id}")
        robot_intervals[fixed.robot_id].append(interval)
        if fixed.zone_id is not None:
            zone_intervals[fixed.zone_id].append(interval)
        available_at[fixed.robot_id] = max(available_at[fixed.robot_id], fixed.end_tick)
        p = progress.get(fixed.job_id)
        if p and p.current_phase:
            initial_location[fixed.robot_id] = p.current_phase.to_zone_id

    values = {}
    chosen = {}
    incoming_arcs = {}
    try:
        for job_id in pending:
            job = jobs[job_id]
            is_continuation = job.lifecycle == "COMMITTED"
            p = progress.get(job_id)
            resume, ready = "MOVE", now
            if is_continuation:
                if p is None:
                    return finish("MODEL_INVALID", "MISSING_EXECUTION_PROGRESS")
                if any(jobs[x].lifecycle != "COMPLETED" for x in job.predecessor_ids):
                    return finish("MODEL_INVALID", "COMMITTED_PREDECESSOR_NOT_COMPLETED")
                if p.current_phase:
                    ready = max(now, p.current_phase.end_tick)
                    resume = {"MOVE": "SETUP", "SETUP": "WELD"}.get(p.current_phase.phase)
                    if resume is None:
                        return finish("MODEL_INVALID", "RUNNING_WELD_HAS_NEW_ASSIGNMENT")
                elif p.setup_valid:
                    resume = "WELD"
                elif any(x.phase == "MOVE" for x in p.completed_phases):
                    resume = "SETUP"
                else:
                    return finish("MODEL_INVALID", "UNKNOWN_CONTINUATION_PREFIX")
            wstart = model.new_int_var(now, horizon, f"ws_{job_id}")
            wend = model.new_int_var(now, horizon, f"we_{job_id}")
            model.add(wend == wstart + job.duration_ticks)
            ms = me = ss = se = duration = None
            if resume == "MOVE":
                ms = model.new_int_var(now, horizon, f"ms_{job_id}")
                me = model.new_int_var(now, horizon, f"me_{job_id}")
                duration = model.new_int_var(0, max(travel.values(), default=0), f"travel_{job_id}")
                model.add(me == ms + duration)
            if resume in ("MOVE", "SETUP"):
                ss = model.new_int_var(ready, horizon, f"ss_{job_id}")
                se = model.new_int_var(ready, horizon, f"se_{job_id}")
                model.add(ss >= me if resume == "MOVE" else ss >= ready)
                model.add(se == ss + job.setup_ticks)
                model.add(wstart == se)
            else:
                model.add(wstart == ready)
            suffix_start = ms if resume == "MOVE" else ready
            zone_start = ss if ss is not None else wstart
            rlength = model.new_int_var(0, horizon - now, f"rlength_{job_id}")
            zlength = model.new_int_var(0, horizon - now, f"zlength_{job_id}")
            model.add(rlength == wend - suffix_start)
            model.add(zlength == wend - zone_start)
            zone_intervals[job.zone_id].append(model.new_interval_var(zone_start, zlength, wend, f"zone_{job_id}"))
            presence = []
            for robot_id in constraints.allowed_assignments.get(job_id, []):
                literal = model.new_bool_var(f"assign_{job_id}_{robot_id}")
                chosen[job_id, robot_id] = literal
                presence.append(literal)
                robot_intervals[robot_id].append(model.new_optional_interval_var(
                    suffix_start, rlength, wend, literal, f"robot_{job_id}_{robot_id}"))
                if is_continuation and robot_id != p.robot_id:
                    model.add(literal == 0)
            model.add_exactly_one(presence)
            values[job_id] = dict(ms=ms, me=me, ss=ss, se=se, ws=wstart, we=wend,
                duration=duration, begin=suffix_start, resume=resume, progress=p,
                continuation=is_continuation)

        # Dummy node 0 joins the last job back to the first without a time
        # constraint. Self-loops remove jobs not assigned to this robot.
        for robot_id in sorted(robots):
            candidates = [j for j in pending if (j, robot_id) in chosen]
            if not candidates:
                continue
            nodes = {j: n for n, j in enumerate(candidates, 1)}
            empty = model.new_bool_var(f"empty_{robot_id}")
            model.add(sum(chosen[j, robot_id] for j in candidates) == 0).only_enforce_if(empty)
            model.add(sum(chosen[j, robot_id] for j in candidates) >= 1).only_enforce_if(empty.Not())
            arcs = [(0, 0, empty)]
            for job_id in candidates:
                n, v = nodes[job_id], values[job_id]
                present = chosen[job_id, robot_id]
                arcs.append((n, n, present.Not()))
                first = model.new_bool_var(f"first_{robot_id}_{job_id}")
                last = model.new_bool_var(f"last_{robot_id}_{job_id}")
                arcs.extend([(0, n, first), (n, 0, last)])
                model.add_implication(first, present)
                model.add_implication(last, present)
                model.add(v["begin"] >= available_at[robot_id]).only_enforce_if(first)
                incoming_arcs[robot_id, job_id, None] = first
                if v["continuation"]:
                    # Preserve an already-started attempt rather than wasting
                    # its setup or treating its robot as idle.
                    model.add(first == present)
                else:
                    model.add(v["duration"] == travel[initial_location[robot_id], jobs[job_id].zone_id]).only_enforce_if(first)
                for previous in candidates:
                    if previous == job_id:
                        continue
                    arc = model.new_bool_var(f"arc_{robot_id}_{previous}_{job_id}")
                    arcs.append((nodes[previous], n, arc))
                    model.add_implication(arc, present)
                    model.add_implication(arc, chosen[previous, robot_id])
                    incoming_arcs[robot_id, job_id, previous] = arc
                    if v["continuation"]:
                        model.add(arc == 0)
                    else:
                        model.add(v["ms"] >= values[previous]["we"]).only_enforce_if(arc)
                        model.add(v["duration"] == travel[jobs[previous].zone_id, jobs[job_id].zone_id]).only_enforce_if(arc)
            model.add_circuit(arcs)

        for intervals in robot_intervals.values():
            model.add_no_overlap(intervals)
        for intervals in zone_intervals.values():
            model.add_no_overlap(intervals)
        for job_id in pending:
            child = values[job_id]
            for pred_id in jobs[job_id].predecessor_ids:
                pred = jobs[pred_id]
                if pred.lifecycle == "COMPLETED":
                    end = pred.actual_weld_end
                elif pred_id in fixed_welds:
                    end = fixed_welds[pred_id].end_tick
                else:
                    end = values[pred_id]["we"]
                model.add(child["begin"] >= end)
        immediate_literals = []
        for job_id, robot_id in constraints.immediate_start_pairs:
            if (job_id, robot_id) not in chosen or values[job_id]["ms"] is None:
                return finish("MODEL_INVALID", "INVALID_IMMEDIATE_PAIR")
            literal = model.new_bool_var(f"immediate_{job_id}_{robot_id}")
            model.add_implication(literal, chosen[job_id, robot_id])
            model.add(values[job_id]["ms"] == now).only_enforce_if(literal)
            immediate_literals.append(literal)
        if immediate_literals:
            model.add(sum(immediate_literals) >= 1)

        tardiness, assignment_changes, start_changes, completion_sum = [], [], [], []
        baseline = {a.job_id: a for a in snapshot.baseline_plan.assignments} if snapshot.baseline_plan else {}
        for job_id in constraints.required_job_ids:
            job = jobs[job_id]
            end = fixed_welds[job_id].end_tick if job_id in fixed_welds else values[job_id]["we"]
            late = model.new_int_var(0, max(0, horizon - job.due_tick), f"late_{job_id}")
            model.add_max_equality(late, [end - job.due_tick, 0])
            tardiness.append(job.weight * late)
            completion_sum.append(end)
            old = baseline.get(job_id)
            if job_id in values and not values[job_id]["continuation"] and old and (job_id, old.robot_id) in chosen:
                assignment_changes.append(1 - chosen[job_id, old.robot_id])
                if old.weld_start is not None and old.weld_start >= now:
                    shift = model.new_int_var(0, horizon + old.weld_start, f"shift_{job_id}")
                    model.add_abs_equality(shift, values[job_id]["ws"] - old.weld_start)
                    start_changes.append(shift)
        objectives = [sum(tardiness), sum(assignment_changes), sum(start_changes), sum(completion_sum)]
    except (KeyError, ValueError, TypeError) as exc:
        return finish("MODEL_INVALID", f"INVALID_MODEL_INPUT:{type(exc).__name__}")

    validation = model.validate()
    if validation:
        return finish("MODEL_INVALID", validation)

    def extract(solver):
        result = []
        for job_id in pending:
            v, job = values[job_id], jobs[job_id]
            robot_id = next(r for r in constraints.allowed_assignments[job_id] if solver.boolean_value(chosen[job_id, r]))
            previous = next(prev for (r, j, prev), arc in incoming_arcs.items()
                if r == robot_id and j == job_id and solver.boolean_value(arc))
            source = jobs[previous].zone_id if previous else initial_location[robot_id]
            if v["continuation"]:
                source = job.zone_id
            result.append(Assignment(
                mode="CONTINUATION" if v["continuation"] else "NEW",
                attempt_id=v["progress"].attempt_id if v["continuation"] else new_id("attempt"),
                resume_phase=v["resume"], job_id=job_id, robot_id=robot_id, zone_id=job.zone_id,
                move_start=None if v["ms"] is None else solver.value(v["ms"]),
                move_end=None if v["me"] is None else solver.value(v["me"]),
                setup_start=None if v["ss"] is None else solver.value(v["ss"]),
                setup_end=None if v["se"] is None else solver.value(v["se"]),
                weld_start=solver.value(v["ws"]), weld_end=solver.value(v["we"]),
                predecessor_ids=job.predecessor_ids, source_position=source,
            ))
        return result

    names = ["weighted_tardiness", "robot_changes", "weld_start_shift", "sum_weld_end"]
    for index, objective in enumerate(objectives):
        remaining = policy.solver_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return finish("FEASIBLE_NOT_PROVEN" if assignments else "NO_SOLUTION_WITHIN_LIMIT", "TOTAL_TIME_BUDGET_EXHAUSTED")
        model.minimize(objective)
        try:
            solver, status = _run_solver(model, policy, remaining)
        except (RuntimeError, ValueError) as exc:
            return finish("INTERNAL_ERROR", f"SOLVER_EXECUTION_FAILED:{type(exc).__name__}")
        raw_status = solver.status_name(status)
        has_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        stages.append(dict(name=names[index], status=raw_status,
            objective=round(solver.objective_value) if has_solution else None,
            best_bound=float(solver.best_objective_bound), wall_time_ms=float(solver.wall_time) * 1000))
        if has_solution:
            assignments = extract(solver)
            best_values = [int(solver.value(o)) if not isinstance(o, int) else o for o in objectives]
            if status == cp_model.FEASIBLE:
                return finish("FEASIBLE_NOT_PROVEN", "INCUMBENT_WITHOUT_FULL_LEXICOGRAPHIC_PROOF")
            proven += 1
            model.add(objective == best_values[index])
        elif status == cp_model.UNKNOWN:
            return finish("FEASIBLE_NOT_PROVEN" if assignments else "NO_SOLUTION_WITHIN_LIMIT", "SEARCH_LIMIT")
        elif status == cp_model.INFEASIBLE:
            return finish("INTERNAL_ERROR" if assignments else "INFEASIBLE",
                "LEXICOGRAPHIC_FEASIBILITY_LOST" if assignments else "MODEL_INFEASIBLE")
        elif status == cp_model.MODEL_INVALID:
            return finish("MODEL_INVALID", "SOLVER_REJECTED_MODEL")
        else:
            return finish("INTERNAL_ERROR", "UNEXPECTED_SOLVER_STATUS")
    return finish("OPTIMAL", "ALL_FOUR_OBJECTIVES_PROVEN")
