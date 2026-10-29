"""Request-local phase timing and lossless evidence ordering."""

from collections import defaultdict, deque
from contextlib import contextmanager
from time import perf_counter


class PhaseMetrics:
    def __init__(self):
        self.started = perf_counter()
        self.calls = []
        self.retrieval_rounds = []

    def call(self, operation, function, *args, **kwargs):
        with self.measure(operation):
            return function(*args, **kwargs)

    @contextmanager
    def measure(self, operation):
        started, succeeded = perf_counter(), False
        try:
            yield
            succeeded = True
        finally:
            self.calls.append(
                {
                    "operation": operation,
                    "seconds": round(perf_counter() - started, 6),
                    "succeeded": succeeded,
                }
            )

    def summary(self):
        totals = defaultdict(float)
        for call in self.calls:
            totals[call["operation"]] += call["seconds"]
        return {
            "stage": "performance",
            "scope": "agent_before_save",
            "elapsed_seconds": round(perf_counter() - self.started, 6),
            "operations": {k: round(v, 6) for k, v in totals.items()},
            "calls": list(self.calls),
            "model_tasks": sum(c["operation"].startswith("chat.") for c in self.calls),
            "retrieval_rounds": list(self.retrieval_rounds),
        }


def balanced_evidence(items, query, analyzer):
    """Interleave ranked chunks so one long chunk cannot monopolize a pack.

    Lexical relevance orders spans inside each chunk; it is never a support
    decision. Whole original spans and their exact text are preserved.
    """
    query_terms = {t for t in analyzer.analyze(query) if len(t) > 1}
    groups = defaultdict(list)
    for item in items:
        groups[item.chunk_id].append(item)
    result = []
    for heading in (False, True):
        queues = []
        for values in groups.values():
            ranked = [item for item in values if item.is_heading == heading]
            ranked.sort(key=lambda item: -len(query_terms & set(analyzer.analyze(item.text))))
            queues.append(deque(ranked))
        while any(queues):
            for queue in queues:
                if queue:
                    result.append(queue.popleft())
    return result
