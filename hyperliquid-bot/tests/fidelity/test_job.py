"""Tests for the async job wrapper."""
import time

from bot.fidelity import job


def test_start_job_returns_uuid_string():
    jid = job.start_check_job(lambda: 42)
    assert isinstance(jid, str) and len(jid) > 0


def test_job_completes_with_result():
    jid = job.start_check_job(lambda: 1234)
    for _ in range(50):
        rec = job.get_job(jid)
        if rec and rec["status"] == "done":
            break
        time.sleep(0.05)
    rec = job.get_job(jid)
    assert rec["status"] == "done"
    assert rec["result"] == 1234


def test_job_records_error():
    def boom():
        raise RuntimeError("nope")
    jid = job.start_check_job(boom)
    for _ in range(50):
        rec = job.get_job(jid)
        if rec and rec["status"] == "error":
            break
        time.sleep(0.05)
    rec = job.get_job(jid)
    assert rec["status"] == "error"
    assert "nope" in rec["error"]
