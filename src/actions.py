"""Shared operations called by BOTH cli.py and the web UI. No business logic lives in either of them;
all status changes go through db.transition()."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

from . import config, db, digest, drafts as draftsmod, ingest, pipeline, report, tailor as tailormod
from .llm import LLM


class ActionError(Exception):
    pass


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _require(conn, job_id: int):
    job = db.get_job(conn, job_id)
    if job is None:
        raise ActionError(f"no job {job_id}")
    return job


# --------------------------------------------------------------------- review decisions
REVIEW_ACTIONS = {"shortlist": "shortlisted", "skip": "skipped", "ineligible": "ineligible"}


def review(conn, job_id: int, action: str, reason: str = "") -> str:
    """Inbox decision on a scored / needs_review job: shortlist | skip | ineligible."""
    job = _require(conn, job_id)
    if action not in REVIEW_ACTIONS:
        raise ActionError(f"unknown action {action}")
    target = REVIEW_ACTIONS[action]
    if job["status"] == "needs_review" and target in ("shortlisted", "skipped"):
        db.transition(conn, job_id, "scored", f"user reviewed: {reason or 'ok to score'}")
    db.transition(conn, job_id, target, f"user: {action}" + (f" ({reason})" if reason else ""))
    return target


def mark_applied(conn, job_id: int, follow_up_days: int | None = None) -> str:
    _require(conn, job_id)
    days = follow_up_days if follow_up_days is not None else config.load_prefs()["follow_up_days"]
    db.transition(conn, job_id, "applied", "user submitted application manually")
    follow = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
    conn.execute("UPDATE jobs SET follow_up_on=? WHERE id=?", (follow, job_id))
    conn.commit()
    return follow


def set_status(conn, job_id: int, new_status: str, reason: str = "") -> None:
    """Tracker status change (interviewing / offer / rejected / ghosted)."""
    if new_status not in ("interviewing", "offer", "rejected", "ghosted"):
        raise ActionError(f"status {new_status} cannot be set from the tracker")
    _require(conn, job_id)
    db.transition(conn, job_id, new_status, f"user: {reason or new_status}")


def set_follow_up(conn, job_id: int, date: str | None) -> None:
    _require(conn, job_id)
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ActionError("date must be YYYY-MM-DD")
    conn.execute("UPDATE jobs SET follow_up_on=? WHERE id=?", (date or None, job_id))
    conn.commit()
    db.log_event(conn, job_id, f"follow-up date set to {date}")


def contact_sent(conn, job_id: int) -> None:
    """Records that YOU sent a LinkedIn/referral message. The pipeline never sends anything."""
    _require(conn, job_id)
    conn.execute("""UPDATE approvals SET status='approved', decided_at=? WHERE item_id=? AND type='contact_sent'
                    AND status='pending'""", (db.now(), str(job_id)))
    conn.commit()
    db.log_event(conn, job_id, "contact sent (manual)")


def sweep_ghosted(conn, grace_days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).strftime("%Y-%m-%d")
    n = 0
    for r in conn.execute("SELECT id FROM jobs WHERE status='applied' AND follow_up_on IS NOT NULL AND follow_up_on<=?", (cutoff,)).fetchall():
        db.transition(conn, r["id"], "ghosted", f"no response {grace_days}+ days after follow-up date")
        n += 1
    return n


# --------------------------------------------------------------------- generation
def add_url(conn, llm: LLM, url: str) -> tuple[int, str]:
    """Same path as `hub add-url`: safe fetch -> upsert -> synchronous scoring."""
    try:
        job_id = ingest.add_url_job(conn, url, ingest.load_watchlist())
    except (ingest.UnsafeURL, Exception) as e:  # noqa: BLE001
        raise ActionError(f"could not add URL: {e}") from e
    return job_id, pipeline.process_job(conn, llm, job_id)


def tailor(conn, llm: LLM, job_id: int, force: bool = False) -> dict:
    _require(conn, job_id)
    res = tailormod.tailor_job(conn, llm, job_id, force=force)
    report.build(conn, job_id)
    return res


def draft(conn, llm: LLM, job_id: int, questions: list[str] | None = None) -> dict:
    _require(conn, job_id)
    res = draftsmod.generate(conn, llm, job_id, questions)
    report.build(conn, job_id)
    return res


# --------------------------------------------------------------------- proposals
def decide_proposal(conn, approval_id: int, approve: bool) -> None:
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if row is None or row["status"] != "pending":
        raise ActionError("proposal not found or already decided")
    conn.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?",
                 ("approved" if approve else "rejected", db.now(), approval_id))
    conn.commit()  # NOTE: approving never edits facts.yaml — you write the fact by hand.


# --------------------------------------------------------------------- audit & golden
def audit_sample(conn, n: int | None = None) -> list[dict]:
    n = n or config.load_prefs()["audit_sample_size"]
    rows = conn.execute("""SELECT id, title, company, status, status_reason, url FROM jobs
                           WHERE status IN ('filtered_out','ineligible') ORDER BY RANDOM() LIMIT ?""", (n,)).fetchall()
    return [dict(r) for r in rows]


def save_golden(conn, job_id: int, label: str = "apply") -> str:
    """'I would have applied': save the posting as a labelled case in tests/golden/."""
    if label not in ("apply", "maybe", "skip"):
        raise ActionError("label must be apply|maybe|skip")
    job = _require(conn, job_id)
    config.GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = config.GOLDEN_DIR / f"job-{job_id}.json"
    path.write_text(json.dumps({
        "label": label, "source_status": job["status"], "reason_rejected": job["status_reason"],
        "job": {"title": job["title"], "company": job["company"], "location_raw": job["location_raw"],
                "description": job["description"]}}, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------- read models
def inbox(conn, bucket: str | None = None, decision: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT j.*, s.decision AS decision, s.result_json AS ev_json FROM jobs j
           LEFT JOIN scores s ON s.id=(SELECT MAX(id) FROM scores WHERE job_id=j.id AND stage='evaluate'
                                       AND description_hash=j.description_hash)
           WHERE j.status IN ('scored','needs_review') AND j.dup_of IS NULL
           ORDER BY j.status DESC, j.first_seen DESC""").fetchall()
    out = []
    for r in rows:
        ev = json.loads(r["ev_json"]) if r["ev_json"] else {}
        d = dict(r)
        d.pop("description", None)
        d["scores"] = {k: ev[k]["score"] for k in ("skills_match", "seniority_fit", "domain", "timezone_overlap", "red_flags") if k in ev}
        d["pay_status"] = ev.get("pay_status") or "not_stated"
        if r["status"] == "scored" and (r["decision"] or "skip") == "skip":
            continue  # inbox shows apply / maybe and the needs_review queue
        if bucket and r["bucket"] != bucket:
            continue
        if decision and r["decision"] != decision:
            continue
        out.append(d)
    return out


def tracker(conn) -> list[dict]:
    rows = conn.execute("""SELECT id, title, company, status, follow_up_on, status_reason FROM jobs
                           WHERE status IN ('applied','interviewing','offer','rejected','ghosted')
                           ORDER BY follow_up_on IS NULL, follow_up_on""").fetchall()
    today = _today()
    return [{**dict(r), "overdue": bool(r["follow_up_on"] and r["follow_up_on"] <= today and r["status"] == "applied")} for r in rows]


def export_csv(conn) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "company", "title", "bucket", "status", "applied_at", "follow_up_on", "url"])
    for r in conn.execute("""SELECT j.*, (SELECT MIN(at) FROM job_events WHERE job_id=j.id AND to_status='applied') AS applied_at
                             FROM jobs j WHERE status IN ('applied','interviewing','offer','rejected','ghosted') ORDER BY id"""):
        w.writerow([r["id"], r["company"], r["title"], r["bucket"], r["status"], r["applied_at"], r["follow_up_on"], r["url"]])
    return buf.getvalue()


def job_detail(conn, job_id: int) -> dict:
    job = _require(conn, job_id)
    h = job["description_hash"]
    return {
        "job": dict(job),
        "rules": db.score_json(db.latest_score(conn, job_id, "rules", h)),
        "eligibility": db.score_json(db.latest_score(conn, job_id, "eligibility", h)),
        "evaluate": db.score_json(db.latest_score(conn, job_id, "evaluate", h)),
        "evaluate_decision": (db.latest_score(conn, job_id, "evaluate", h) or {"decision": None})["decision"],
        "tailor": db.score_json(db.latest_score(conn, job_id, "tailor", h)),
        "drafts": db.score_json(db.latest_score(conn, job_id, "drafts", h)),
        "has_pdf": db.get_artifact(conn, job_id, "resume_pdf") is not None,
        "events": [dict(e) for e in conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,))],
        "answer_yourself": draftsmod.answer_yourself(config.load_yaml("profile.yaml")),
    }


def proposals(conn) -> list[dict]:
    return [{**dict(r), "payload": json.loads(r["payload"] or "{}")}
            for r in conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id")]


def health(conn) -> dict:
    runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 15")]
    return {"runs": runs, "sources": digest.source_health(conn), "spend": digest.spend(conn),
            "audit": audit_sample(conn)}
