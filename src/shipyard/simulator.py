"""Private event queue, returning only arrivals due at the requested tick."""
import heapq
from pathlib import Path

from .contracts import ArrivalBatch, Delivery, Observation, parse_json, new_id


class Simulator:
    def __init__(self, deliveries: list[Delivery]):
        self._future = [(d.delivery_tick, n, d) for n, d in enumerate(deliveries)]
        heapq.heapify(self._future)
        self._ends = []
        self._sequence = 0
        self._accepted = set()

    @classmethod
    def load(cls, path: Path, run_id: str):
        deliveries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            delivery = Delivery.model_validate(parse_json(line))
            if delivery.delivery_tick != delivery.event.received_tick:
                raise ValueError("delivery/event time mismatch")
            delivery.event.run_id = run_id
            deliveries.append(delivery)
        return cls(deliveries)

    def register_completion(self, observation: Observation, end_tick: int):
        if observation.execution_id in self._accepted:
            return
        self._accepted.add(observation.execution_id)
        end = observation.model_copy(update={"observation_id": new_id("observation"), "kind":"PHASE_ENDED", "tick":end_tick, "expected_state_version":None})
        self._sequence += 1
        heapq.heappush(self._ends, (end_tick, self._sequence, end))

    def due_events(self, tick: int) -> ArrivalBatch:
        deliveries, observations = [], []
        while self._ends and self._ends[0][0] <= tick:
            observations.append(heapq.heappop(self._ends)[2])
        while self._future and self._future[0][0] <= tick:
            deliveries.append(heapq.heappop(self._future)[2])
        return ArrivalBatch(tick=tick, observations=observations, deliveries=deliveries)

    def next_tick(self) -> int | None:
        ticks = [q[0][0] for q in (self._future, self._ends) if q]
        return min(ticks) if ticks else None
