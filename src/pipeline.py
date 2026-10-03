"""Fixed pipeline: ingest -> filter -> eligibility -> evaluate -> report -> digest (plan.md §1, §8).
Every step is idempotent and one failing job/source never aborts the run."""
from __future__ import annotations

import logging

from . import db, digest, evaluate, facts as factsmod, filter as filt, ingest, report
from .llm import LLM

log = logging.getLogger("hub.pipeline")


def process_job(conn, llm: LLM, job_id: int, bank=None) -> str:
    """Run one `new` job through rules -> eligibility -> evaluate. Returns the resulting status."""
    job = db.get_job(conn, job_id)
    if job["status"] != "new":
        return job["status"]
    try:
        if filt.filter_job(conn, job) != "pass":
            report.build(conn, job_id)
            return db.get_job(conn, job_id)["status"]
        status = evaluate.score_job(conn, llm, job, bank)
        report.build(conn, job_id)
        return status
    except Exception as e:  # noqa: BLE001 - isolate per-job failures
        log.exception("job %s failed", job_id)
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:500], job_id))
        conn.commit()
        return "error"


def process_new(conn, llm: LLM) -> dict:
    bank = factsmod.load()
    ids = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status='new' AND dup_of IS NULL ORDER BY id")]
    counts: dict[str, int] = {}
    for jid in ids:
        s = process_job(conn, llm, jid, bank)
        counts[s] = counts.get(s, 0) + 1
    return counts


def run(conn, llm: LLM, *, fetch: bool = True, notify: bool = True) -> dict:
    sources = ingest.run_all(conn) if fetch else []
    counts = process_new(conn, llm)
    path = digest.write(conn, notify=notify)
    return {"sources": sources, "processed": counts, "digest": path}
