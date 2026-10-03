"""Stage 1: deterministic rules (rules.py) applied to `new` jobs. Every rejection records a reason."""
from __future__ import annotations

from . import config, db, rules


def filter_job(conn, job) -> str:
    """Returns pass | filtered_out | needs_review. Idempotent per (job, description_hash)."""
    prior = db.latest_score(conn, job["id"], "rules", job["description_hash"])
    if prior is not None:
        return prior["decision"]
    prefs, profile = config.load_prefs(), config.load_yaml("profile.yaml")
    res = rules.apply_rules(dict(job), prefs, profile)
    db.add_score(conn, job["id"], "rules", {"flags": [f.as_dict() for f in res.flags]}, res.decision,
                 description_hash=job["description_hash"])
    if res.decision == "reject":
        db.transition(conn, job["id"], "filtered_out", res.reason)
        return "filtered_out"
    if res.decision == "review":
        db.transition(conn, job["id"], "needs_review", res.reason)
        return "needs_review"
    return "pass"
