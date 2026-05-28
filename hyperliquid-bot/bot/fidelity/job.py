"""Threaded job registry for fidelity checker (mirrors engine.py pattern)."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Any

_jobs: dict = {}
_jobs_lock = threading.Lock()


def start_check_job(fn: Callable[[], Any], description: str = "") -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "description": description,
            "started_at": time.time(),
            "result": None,
            "error": None,
            "elapsed_s": None,
        }

    def _runner():
        started = time.time()
        try:
            result = fn()
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
                _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
                _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)

    threading.Thread(target=_runner, daemon=True, name=f"fidelity-{job_id[:8]}").start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return dict(_jobs[job_id]) if job_id in _jobs else None
