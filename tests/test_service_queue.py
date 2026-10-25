"""Durability, contention and retry boundary tests for the service queue."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from docqa.domain.errors import DocQAError
from docqa.service.queue import JobQueue


@pytest.fixture
def clock():
    now = [1000.0]
    return now


@pytest.fixture
def queue(tmp_path, clock):
    return JobQueue(tmp_path / "state", clock=lambda: clock[0])


def assert_code(code, fn):
    with pytest.raises(DocQAError) as caught:
        fn()
    assert caught.value.code == code


def test_fifo_priority_and_durable_completed_result(queue, clock):
    low = queue.enqueue("scan", {"directory": "文档"}, "scan:1", priority=20)
    clock[0] += 1
    high = queue.enqueue("parse", {"path": "one.pdf"}, "parse:1", priority=1)
    claimed = queue.claim("worker")
    assert claimed["job_id"] == high["job_id"]
    assert claimed["attempt"] == 1
    assert claimed["heartbeat_at"] == clock[0]
    clock[0] += 2
    assert queue.heartbeat(high["job_id"], "worker")["updated_at"] == clock[0]
    done = queue.finish(high["job_id"], "worker", {"artifact_id": "a"})
    assert done["state"] == "succeeded"
    assert done["owner"] is None
    reopened = JobQueue(queue.root)
    assert reopened.get(high["job_id"])["result"] == {"artifact_id": "a"}
    assert queue.claim("worker")["job_id"] == low["job_id"]
    assert queue.claim("worker") is None


def test_dedupe_merge_successor_and_no_concurrent_same_key(queue):
    first = queue.enqueue("scan", {"version": 1}, "watch:1")
    assert queue.enqueue("scan", {"version": 2}, "watch:1")["job_id"] == first["job_id"]
    assert queue.get(first["job_id"])["payload"] == {"version": 1}
    queue.claim("worker")
    successor = queue.enqueue("scan", {"version": 3}, "watch:1")
    assert successor["job_id"] != first["job_id"]
    assert queue.enqueue("scan", {}, "watch:1")["job_id"] == successor["job_id"]
    assert queue.claim("other") is None
    failed = queue.fail(first["job_id"], "worker", "MODEL_UNAVAILABLE", retryable=True)
    assert failed["state"] == "failed"
    assert failed["error_code"] == "MODEL_UNAVAILABLE"
    assert failed["superseded_by"] == successor["job_id"]
    assert queue.claim("worker")["job_id"] == successor["job_id"]


def test_finite_exponential_retry(clock, tmp_path):
    queue = JobQueue(tmp_path, clock=lambda: clock[0], retry_base_seconds=5)
    job = queue.enqueue("parse", {}, "file:1")
    for attempt, delay in ((1, 5), (2, 10), (3, 0)):
        claimed = queue.claim("worker")
        assert claimed["attempt"] == attempt
        failed = queue.fail(job["job_id"], "worker", "MODEL_TIMEOUT", retryable=True)
        if attempt < 3:
            assert failed["state"] == "retry_wait"
            assert failed["available_at"] == clock[0] + delay
            assert queue.claim("worker") is None
            clock[0] += delay
        else:
            assert failed["state"] == "failed"
            assert queue.claim("worker") is None


def test_cancel_and_retry_reset_only_terminal_failures(queue):
    job = queue.enqueue("parse", {}, "one")
    cancelled = queue.cancel(job["job_id"])
    assert cancelled["state"] == "cancelled"
    assert queue.cancel(job["job_id"])["state"] == "cancelled"
    retried = queue.retry(job["job_id"])
    assert retried["attempt"] == 0
    assert retried["state"] == "queued"
    assert_code("SERVICE_JOB_NOT_RETRYABLE", lambda: queue.retry(job["job_id"]))
    queue.claim("worker")
    assert_code("SERVICE_JOB_RUNNING", lambda: queue.cancel(job["job_id"]))
    queue.finish(job["job_id"], "worker", {})
    assert queue.cancel(job["job_id"])["state"] == "succeeded"
    assert_code("SERVICE_JOB_NOT_RETRYABLE", lambda: queue.retry(job["job_id"]))


def test_explicit_retry_deduplication_and_capacity(queue):
    failed = queue.enqueue("parse", {}, "one")
    queue.claim("worker")
    queue.fail(failed["job_id"], "worker", "PARSE_FAILED")
    other = queue.enqueue("parse", {"revision": 2}, "one")
    assert queue.retry(failed["job_id"])["job_id"] == other["job_id"]
    assert queue.get(failed["job_id"])["state"] == "failed"


def test_queue_capacity_includes_running_and_waiting(tmp_path):
    queue = JobQueue(tmp_path, max_pending=1)
    job = queue.enqueue("scan", {}, "one")
    assert queue.enqueue("scan", {}, "one")["job_id"] == job["job_id"]
    assert_code("SERVICE_QUEUE_FULL", lambda: queue.enqueue("scan", {}, "two"))
    queue.claim("worker")
    assert_code("SERVICE_QUEUE_FULL", lambda: queue.enqueue("scan", {}, "one"))
    queue.finish(job["job_id"], "worker", {})
    assert queue.enqueue("scan", {}, "two")["state"] == "queued"
    queue.cancel(queue.list(state="queued")[0]["job_id"])
    assert queue.stats()["active"] == 0


@pytest.mark.parametrize("operation", ["heartbeat", "finish", "fail"])
def test_worker_ownership(queue, operation):
    job = queue.enqueue("parse", {}, "one")
    queue.claim("right")
    extra = {"heartbeat": (), "finish": ({},), "fail": ("PARSE_FAILED",)}[operation]
    assert_code(
        "SERVICE_JOB_NOT_OWNED", lambda: getattr(queue, operation)(job["job_id"], "wrong", *extra)
    )
    assert queue.get(job["job_id"])["state"] == "running"


def test_recovery_requeues_preserves_attempt_and_never_uses_ttl(queue, clock):
    job = queue.enqueue("parse", {}, "one")
    queue.claim("gone")
    clock[0] += 10_000_000
    assert queue.claim("new") is None
    assert queue.recover_running() == 1
    assert queue.get(job["job_id"])["attempt"] == 1
    assert queue.get(job["job_id"])["owner"] is None
    assert queue.claim("new")["attempt"] == 2
    queue.recover_running()
    assert queue.claim("new")["attempt"] == 3
    queue.recover_running()
    assert queue.get(job["job_id"])["state"] == "failed"
    assert queue.get(job["job_id"])["error_code"] == "SERVICE_WORKER_INTERRUPTED"
    assert queue.claim("new") is None


def test_recovery_with_successor_preserves_latest_work(queue):
    first = queue.enqueue("parse", {}, "one")
    queue.claim("gone")
    successor = queue.enqueue("parse", {"new": True}, "one")
    assert queue.recover_running() == 1
    assert queue.get(first["job_id"])["state"] == "failed"
    assert queue.get(first["job_id"])["superseded_by"] == successor["job_id"]
    assert queue.claim("new")["job_id"] == successor["job_id"]


def test_atomic_concurrent_enqueue_and_claim(queue):
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: queue.enqueue("scan", {}, "watch:one"), range(16)))
    assert len({job["job_id"] for job in results}) == 1
    for number in range(8):
        queue.enqueue("parse", {"number": number}, f"file:{number}")
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda n: queue.claim(f"worker:{n}"), range(16)))
    claimed = [job for job in claims if job]
    assert len(claimed) == 9
    assert len({job["job_id"] for job in claimed}) == 9
    assert queue.stats()["states"]["running"] == 9


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"x": {"api_key": "do-not-store"}},
        {"AUTHORIZATION": "do-not-store"},
        {"x": float("nan")},
        {"x": float("inf")},
        {1: "key"},
        {"x": (1, 2)},
        {"oversized": "图" * 22_000},
        {"x": "\ud800"},
    ],
)
def test_invalid_json_never_persisted(queue, payload):
    assert_code("SERVICE_INVALID_JOB", lambda: queue.enqueue("parse", payload, "one"))
    assert queue.stats()["total"] == 0


def test_result_validation_keeps_running_for_explicit_failure(queue):
    job = queue.enqueue("parse", {}, "one")
    queue.claim("worker")
    assert_code(
        "SERVICE_INVALID_JOB", lambda: queue.finish(job["job_id"], "worker", {"password": "hidden"})
    )
    assert queue.get(job["job_id"])["state"] == "running"


@pytest.mark.parametrize("code", ["Exception: user secret", "bad", "", "A" * 129, None])
def test_only_error_codes_not_exception_bodies(queue, code):
    job = queue.enqueue("parse", {}, "one")
    queue.claim("worker")
    assert_code("SERVICE_INVALID_JOB", lambda: queue.fail(job["job_id"], "worker", code))
    assert queue.get(job["job_id"])["error_code"] is None


def test_invalid_ids_and_missing_job(queue):
    assert_code("SERVICE_INVALID_JOB", lambda: queue.get("../../elsewhere"))
    assert_code("SERVICE_JOB_NOT_FOUND", lambda: queue.get("a" * 32))
    assert_code("SERVICE_INVALID_JOB", lambda: queue.list(limit=0))
    assert_code("SERVICE_INVALID_JOB", lambda: queue.list(state="unknown"))
    assert_code("SERVICE_INVALID_JOB", lambda: queue.claim(" "))


def test_conflicting_kind_and_sql_characters_are_safe(queue):
    key = "scan:'; DELETE FROM jobs; --"
    first = queue.enqueue("scan", {}, key)
    assert_code("SERVICE_DEDUPE_CONFLICT", lambda: queue.enqueue("parse", {}, key))
    assert queue.get(first["job_id"])["dedupe_key"] == key
    assert queue.stats()["total"] == 1
    queue.claim("worker")
    assert_code("SERVICE_DEDUPE_CONFLICT", lambda: queue.enqueue("parse", {}, key))


def test_lowered_attempt_budget_does_not_hide_runnable_job(tmp_path, clock):
    queue = JobQueue(tmp_path, clock=lambda: clock[0])
    exhausted = queue.enqueue("parse", {}, "old")
    queue.claim("worker")
    queue.fail(exhausted["job_id"], "worker", "MODEL_TIMEOUT", retryable=True)
    ready = queue.enqueue("scan", {}, "new")
    lower_limit = JobQueue(tmp_path, clock=lambda: clock[0], max_attempts=1)
    assert lower_limit.claim("worker")["job_id"] == ready["job_id"]
    assert lower_limit.get(exhausted["job_id"])["state"] == "failed"


def test_invalid_unicode_key_and_unhashable_filter(queue):
    assert_code("SERVICE_INVALID_JOB", lambda: queue.enqueue("scan", {}, "\ud800"))
    assert_code("SERVICE_INVALID_JOB", lambda: queue.list(state=[]))


def test_retry_cannot_merge_into_different_task_kind(queue):
    old = queue.enqueue("scan", {}, "shared")
    queue.cancel(old["job_id"])
    queue.enqueue("parse", {}, "shared")
    assert_code("SERVICE_DEDUPE_CONFLICT", lambda: queue.retry(old["job_id"]))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_pending": 0},
        {"max_pending": True},
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"retry_base_seconds": float("inf")},
        {"retry_base_seconds": -1},
    ],
)
def test_invalid_options(tmp_path, kwargs):
    assert_code("SERVICE_INVALID_JOB", lambda: JobQueue(tmp_path, **kwargs))


def test_deep_json_and_bad_clock(queue, tmp_path):
    node = {}
    for _ in range(34):
        node = {"child": node}
    assert_code("SERVICE_INVALID_JOB", lambda: queue.enqueue("scan", node, "deep"))
    invalid_clock = JobQueue(tmp_path / "badclock", clock=lambda: float("nan"))
    assert_code("SERVICE_INVALID_JOB", lambda: invalid_clock.enqueue("scan", {}, "one"))


def test_corrupt_persisted_payload_reports_stable_error(queue):
    job = queue.enqueue("scan", {}, "one")
    with sqlite3.connect(str(queue.path)) as db:
        db.execute("UPDATE jobs SET payload='broken' WHERE job_id=?", (job["job_id"],))
    assert_code("SERVICE_STATE_CORRUPT", lambda: queue.get(job["job_id"]))


def test_very_large_backoff_is_capped(tmp_path, clock):
    queue = JobQueue(tmp_path, clock=lambda: clock[0], max_attempts=100, retry_base_seconds=86400)
    job = queue.enqueue("parse", {}, "one")
    for _ in range(5):
        queue.claim("worker")
        failed = queue.fail(job["job_id"], "worker", "MODEL_TIMEOUT", retryable=True)
        assert failed["available_at"] == clock[0] + 86400
        clock[0] += 86400


def test_completed_terminal_actions_do_not_touch_timestamp(queue, clock):
    job = queue.enqueue("parse", {}, "one")
    queue.claim("worker")
    done = queue.finish(job["job_id"], "worker", {})
    clock[0] += 100
    assert queue.cancel(job["job_id"])["updated_at"] == done["updated_at"]
    assert_code("SERVICE_JOB_NOT_OWNED", lambda: queue.finish(job["job_id"], "worker", {}))


def test_latest_includes_terminal_records_and_breaks_timestamp_ties(queue):
    assert queue.latest("one") is None
    first = queue.enqueue("parse", {}, "one")
    assert queue.latest("one")["job_id"] == first["job_id"]
    queue.claim("worker")
    queue.fail(first["job_id"], "worker", "PARSE_FAILED")
    assert queue.latest("one")["state"] == "failed"
    second = queue.enqueue("parse", {"revision": 2}, "one")
    assert first["created_at"] == second["created_at"]
    assert queue.latest("one")["job_id"] == second["job_id"]
    queue.cancel(second["job_id"])
    assert queue.latest("one")["state"] == "cancelled"
    assert queue.latest("one")["payload"] == {"revision": 2}
    assert queue.stats()["total"] == 2


def test_latest_prefers_created_time_before_rowid(queue, clock):
    clock[0] = 2000
    first = queue.enqueue("parse", {}, "one")
    queue.cancel(first["job_id"])
    clock[0] = 1000
    queue.enqueue("parse", {}, "one")
    assert queue.latest("one")["job_id"] == first["job_id"]


@pytest.mark.parametrize("key", [None, [], "", " ", "x" * 1025, "\ud800"])
def test_latest_validates_key(queue, key):
    assert_code("SERVICE_INVALID_JOB", lambda: queue.latest(key))


def test_latest_uses_parameterized_key(queue):
    key = "'; DROP TABLE jobs; --"
    job = queue.enqueue("parse", {}, key)
    assert queue.latest(key)["job_id"] == job["job_id"]
    assert queue.stats()["total"] == 1
