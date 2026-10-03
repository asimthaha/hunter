"""Tailoring (plan.md §9). The model returns IDs only; code assembles every word of the resume."""
from __future__ import annotations

import logging

from . import config, db, facts as factsmod, prompts, render, validate
from .evaluate import job_text
from .llm import LLM, LLMError, PIIGuardError, SchemaError

log = logging.getLogger("hub.tailor")

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_id": {"type": "string"},
        "variant_ids": {"type": "array", "items": {"type": "string"}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "substitutions": {"type": "array", "items": {"type": "object", "properties": {
            "from": {"type": "string"}, "to": {"type": "string"}}, "required": ["from", "to"]}},
        "missing_evidence": {"type": "array", "items": {"type": "object", "properties": {
            "requirement": {"type": "string"}, "suggestion": {"type": "string"}},
            "required": ["requirement", "suggestion"]}},
    },
    "required": ["summary_id", "variant_ids", "skills", "substitutions", "missing_evidence"],
}


def assemble(sel: dict, bank: factsmod.FactBank) -> dict:
    """Build the resume model. Titles, companies, dates and all text come from facts.yaml."""
    subs = sel.get("substitutions", [])
    by_role: dict[str, list] = {}
    for vid in sel["variant_ids"]:
        v = bank.variants[vid]
        by_role.setdefault(v["role"], []).append({"id": vid, "text": validate.apply_substitutions(v["text"], subs)})
    roles = [{"title": r.get("title"), "company": r.get("company"), "dates": r.get("dates"), "bullets": by_role[rid]}
             for rid, r in bank.roles.items() if rid in by_role]            # facts.yaml order = display order
    return {
        "summary": validate.apply_substitutions(bank.summaries[sel["summary_id"]], subs),
        "skills": [bank.skills[s.lower()]["name"] for s in sel["skills"]],
        "roles": roles,
        "education": bank.education,
        "rank": list(sel["variant_ids"]),
    }


def tailor_job(conn, llm: LLM, job_id: int, *, force: bool = False) -> dict:
    """Returns {ok, errors, ...}. On success the job moves shortlisted -> tailored."""
    job = db.get_job(conn, job_id)
    if job["status"] != "shortlisted":
        return {"ok": False, "errors": [f"job is {job['status']}, not shortlisted"]}
    if job["missed_runs"] > 0 and not force:
        return {"ok": False, "errors": ["job was missing from its source feed on the last run; "
                                         "check it is still open (use force to override)"]}
    bank = factsmod.load()
    if not bank.variants:
        return {"ok": False, "errors": ["data/facts.yaml is missing or empty"]}
    prefs = config.load_prefs()
    version, instructions = prompts.load("tailor")
    try:
        sel = llm.call(stage="tailor", model=config.TAILOR_MODEL, instructions=instructions,
                       context=bank.for_prompt(), user=prompts.wrap_untrusted(job_text(job)),
                       schema=TAILOR_SCHEMA, job_id=job_id, max_tokens=2000)
    except (PIIGuardError, SchemaError, LLMError) as e:
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", (str(e)[:500], job_id))
        conn.commit()
        return {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}

    errors = validate.validate_resume(sel, bank)
    result: dict = {"selection": sel}
    if errors:
        db.add_score(conn, job_id, "tailor", {**result, "errors": errors}, "invalid", model=config.TAILOR_MODEL,
                     prompt_version=version, facts_version=bank.version, description_hash=job["description_hash"])
        return {"ok": False, "errors": errors}

    for me in sel["missing_evidence"]:
        db.add_approval(conn, f"{job_id}:{me['requirement'][:80]}", "fact_bank_edit",
                        {"job_id": job_id, "requirement": me["requirement"], "suggestion": me["suggestion"]})
    contact = config.load_yaml("contact.yaml")
    model = assemble(sel, bank)
    try:
        rendered = render.render_resume(model, contact, config.OUTPUT_DIR / f"{job_id}-resume.pdf",
                                        prefs["resume_max_pages"])
    except Exception as e:  # noqa: BLE001 - e.g. chromium not installed
        err = f"render failed: {type(e).__name__}: {e}"
        db.add_score(conn, job_id, "tailor", {**result, "errors": [err]}, "render_failed", model=config.TAILOR_MODEL,
                     prompt_version=version, facts_version=bank.version, description_hash=job["description_hash"])
        return {"ok": False, "errors": [err]}
    result["render"] = rendered
    db.add_score(conn, job_id, "tailor", result, "ok", model=config.TAILOR_MODEL, prompt_version=version,
                 facts_version=bank.version, description_hash=job["description_hash"])
    db.add_artifact(conn, job_id, "resume_pdf", rendered["path"])
    db.transition(conn, job_id, "tailored", "resume rendered")
    return {"ok": True, "errors": [], **rendered}
