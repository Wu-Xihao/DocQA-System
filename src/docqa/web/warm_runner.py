"""One reusable isolated child per dispatcher; cancellation replaces that child."""

import json
import os
import queue
import subprocess
import sys
import threading
import time

from docqa.domain.errors import DocQAError
from docqa.web.runner import (
    MAX_INPUT_BYTES,
    STAGES,
    HeadlessRunner,
    _result,
    _valid_request,
    verify_saved_result,
)


class WarmRunner(HeadlessRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.process = None
        self.environment = None
        self.reader_threads = []
        self.lock = threading.Lock()
        self.call_lock = threading.Lock()

    def close(self):
        if self.process is not None:
            self._reap(self.process)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()
            for thread in self.reader_threads:
                thread.join(timeout=1)
        self.process = None

    def _start(self, environment):
        self.close()
        self.runs.mkdir(parents=True, exist_ok=True)
        self.lines, self.stages = queue.Queue(maxsize=4), queue.Queue(maxsize=128)
        self.overflow, self.total = threading.Event(), 0
        self.environment = environment
        self.process = subprocess.Popen(
            [sys.executable, "-m", "docqa.web.worker", "--persistent"],
            cwd=self.runs,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process, lines, stages, overflow = self.process, self.lines, self.stages, self.overflow

        def read(pipe, stdout):
            try:
                while raw := pipe.readline(self.max_output_bytes + 2):
                    with self.lock:
                        self.total += len(raw)
                        excessive = self.total > self.max_output_bytes
                    if excessive:
                        overflow.set()
                        return
                    if stdout:
                        try:
                            lines.put_nowait(raw)
                        except queue.Full:
                            overflow.set()
                    elif raw.startswith(b"DOCQA_STAGE:"):
                        value = raw.decode("ascii", errors="ignore").strip().split(":")
                        if len(value) == 3 and value[2] in STAGES:
                            try:
                                stages.put_nowait((value[1], value[2]))
                            except queue.Full:
                                pass
            except (OSError, ValueError):
                pass

        self.reader_threads = [
            threading.Thread(target=read, args=(process.stdout, True), daemon=True),
            threading.Thread(target=read, args=(process.stderr, False), daemon=True),
        ]
        for thread in self.reader_threads:
            thread.start()

    def run(self, job_id, request, *, cancelled, progress):
        if not self.call_lock.acquire(blocking=False):
            raise DocQAError("RUNNER_BUSY", "Worker is already handling a request")
        try:
            return self._run(job_id, request, cancelled=cancelled, progress=progress)
        finally:
            self.call_lock.release()

    def _run(self, job_id, request, *, cancelled, progress):
        selected = _valid_request(request)
        if (
            not isinstance(job_id, str)
            or len(job_id) != 32
            or any(c not in "0123456789abcdef" for c in job_id)
        ):
            raise DocQAError("INVALID_REQUEST", "Invalid worker job identity")
        if cancelled():
            raise DocQAError("WEB_JOB_CANCELLED", "Question cancelled")
        environment = self._environment(chat_only=selected.mode == "chat")
        # Changing credential exposure or model settings replaces the process.
        reused = bool(
            self.process
            and self.process.poll() is None
            and self.environment == environment
            and self.lines.empty()
        )
        if not reused:
            self._start(environment)
        with self.lock:
            self.total = 0
        started, wall = time.monotonic(), time.time()
        directory = self.runs / job_id
        directory.mkdir()
        payload = json.dumps(
            {
                "job_id": job_id,
                "index": str(self.index),
                "agent_state": str(self.agent_state),
                "options": self.options.model_dump(mode="json"),
                "request": selected.model_dump(mode="json"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if len(payload) > MAX_INPUT_BYTES:
            raise DocQAError("RUNNER_INPUT_LIMIT", "Worker input exceeds its byte limit")
        process = self.process
        process_id, stages, failure = process.pid, [], None

        def send():
            try:
                process.stdin.write(payload + b"\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass

        writer = threading.Thread(target=send, daemon=True)
        writer.start()
        try:
            while True:
                if cancelled():
                    raise DocQAError("WEB_JOB_CANCELLED", "Question cancelled")
                if time.monotonic() - started >= self.timeout_seconds:
                    raise DocQAError("RUNNER_TIMEOUT", "Question deadline elapsed")
                if self.overflow.is_set():
                    raise DocQAError("RUNNER_OUTPUT_LIMIT", "Worker output exceeds limit")
                while not self.stages.empty():
                    jid, stage = self.stages.get_nowait()
                    if jid == job_id:
                        progress(stage)
                        if len(stages) < 128:
                            stages.append(
                                {
                                    "stage": stage,
                                    "elapsed_seconds": round(time.monotonic() - started, 3),
                                }
                            )
                try:
                    raw = self.lines.get(timeout=0.025)
                    break
                except queue.Empty:
                    if self.process.poll() is not None:
                        raise DocQAError("RUNNER_PROCESS_FAILED", "Reusable worker exited")
            result = _result(raw, 0)
            progress("checking_result")
            result = verify_saved_result(self.index, self.agent_state, job_id, selected, result)
            if cancelled():
                raise DocQAError("WEB_JOB_CANCELLED", "Question cancelled")
            if time.monotonic() - started >= self.timeout_seconds:
                raise DocQAError("RUNNER_TIMEOUT", "Result validation exceeded deadline")
            progress("complete")
            return result
        except Exception as error:
            failure = error.code if isinstance(error, DocQAError) else "RUNNER_FAILED"
            self.close()
            raise
        finally:
            writer.join(timeout=1)
            (directory / "timing.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "started_at": wall,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "reused": reused,
                        "process_id": process_id,
                        "error_code": failure,
                        "stages": stages,
                    }
                ),
                encoding="utf-8",
            )
