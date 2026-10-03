"""Eligibility gate (Haiku) and rubric evaluation (Sonnet). plan.md §6, §7, §12.

Model output is extraction/scoring only. Quote verification, hard rules, pay comparison and the
final apply/maybe/skip decision all run here, in code.
"""
from __future__ import annotations

import logging
import re

from . import config, db, facts as factsmod, prompts, rules
from .llm import LLM, LLMError, PIIGuardError, SchemaError

log = logging.getLogger("hub.evaluate")


# ------------------------------------------------------------------------------- quote checking
def _norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().casefold()


def verify_quote(quote: str, text: str) -> bool:
    """A quote is valid if empty/unclear (no claim made) or a whitespace-normalised substring of the text."""
    q = _norm(quote)
    if not q or q == "unclear":
        return True
    return q in _norm(text)


# ------------------------------------------------------------------------------- schemas
def _field(value_schema: dict) -> dict:
    return {"type": "object", "properties": {"value": value_schema, "quote": {"type": "string"}},
            "required": ["value", "quote"]}


ELIGIBILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "allowed_regions": _field({"type": "array", "items": {"type": "string"}}),
        "timezone_overlap": _field({"type": "string"}),
        "work_authorization": _field({"type": "string", "enum": [
            "not_stated", "sponsorship_available", "no_sponsorship", "citizens_or_residents_only"]}),
        "employment_type": _field({"type": "string", "enum": ["employee", "contractor", "eor", "not_stated"]}),
        "notice_requirement": {"type": "object", "properties": {
            "max_days": {"type": ["integer", "null"]}, "quote": {"type": "string"}}, "required": ["max_days", "quote"]},
        "pay": {"type": "object", "properties": {
            "stated": {"type": "boolean"}, "amount_min": {"type": ["number", "null"]},
            "amount_max": {"type": ["number", "null"]}, "currency": {"type": ["string", "null"]},
            "period": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "lakh_per_annum", "not_stated"]},
            "quote": {"type": "string"}}, "required": ["stated", "amount_min", "amount_max", "currency", "period", "quote"]},
        "language": {"type": "string"},
        "asks_for_money": _field({"type": "boolean"}),
    },
    "required": ["allowed_regions", "timezone_overlap", "work_authorization", "employment_type",
                 "notice_requirement", "pay", "language", "asks_for_money"],
}

_DIM = {"type": "object", "properties": {
    "score": {"type": "integer", "minimum": 1, "maximum": 5},
    "reason": {"type": "string"}, "quote": {"type": "string"}}, "required": ["score", "reason", "quote"]}
DIMENSIONS = ("skills_match", "seniority_fit", "domain", "timezone_overlap", "red_flags")
EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {**{d: _DIM for d in DIMENSIONS},
                   "bucket": {"type": "string", "enum": ["india", "remote-foreign", "relocation"]},
                   "summary": {"type": "string"}},
    "required": [*DIMENSIONS, "bucket", "summary"],
}


# ------------------------------------------------------------------------------- eligibility
def job_text(job) -> str:
    return f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location_raw']}\n\n{job['description']}"


def _quotes(ext: dict) -> dict[str, str]:
    q = {k: v.get("quote", "") for k, v in ext.items() if isinstance(v, dict)}
    return q


def decide_eligibility(ext: dict, text: str, prefs: dict, profile: dict) -> dict:
    """Pure decision from the model's extraction. Returns {decision, reasons, unverified}."""
    unverified = [k for k, q in _quotes(ext).items() if not verify_quote(q, text)]
    reasons: list[str] = []
    if unverified:
        return {"decision": "unclear", "unverified": unverified,
                "reasons": [f"quote not found in posting for: {', '.join(unverified)}"]}

    if ext["asks_for_money"]["value"]:
        return {"decision": "ineligible", "unverified": [], "reasons": ["posting asks for money"],
                "quote": ext["asks_for_money"]["quote"]}
    if (ext.get("language") or "english").strip().lower() not in ("english", "en"):
        return {"decision": "unclear", "unverified": [], "reasons": [f"non-English posting ({ext['language']})"]}

    mine = {r.lower() for r in prefs.get("eligible_regions", [])}
    regions = [r.lower() for r in ext["allowed_regions"]["value"]]
    region_ok = not regions or any(m in r or r in m for r in regions for m in mine)
    wa = ext["work_authorization"]["value"]
    if not region_ok:
        return {"decision": "ineligible", "unverified": [], "quote": ext["allowed_regions"]["quote"],
                "reasons": [f"allowed regions {regions} exclude your eligible regions"]}
    if wa == "citizens_or_residents_only" and not any(r in ("worldwide", "anywhere", "global", "india") for r in regions):
        return {"decision": "ineligible", "unverified": [], "quote": ext["work_authorization"]["quote"],
                "reasons": ["citizens/residents only"]}
    if ext["employment_type"]["value"] in prefs.get("employment_types_excluded", []):
        return {"decision": "ineligible", "unverified": [], "quote": ext["employment_type"]["quote"],
                "reasons": [f"employment type {ext['employment_type']['value']} excluded"]}
    n = ext["notice_requirement"]
    flag = rules.check_notice(n["max_days"], profile, n["quote"])
    if flag:
        return {"decision": "ineligible", "unverified": [], "quote": n["quote"], "reasons": [flag.reason]}
    return {"decision": "eligible", "unverified": [], "reasons": []}


def extract_eligibility(llm: LLM, job, *, job_id: int | None = None) -> dict:
    version, instructions = prompts.load("eligibility")
    return llm.call(stage="eligibility", model=config.ELIGIBILITY_MODEL, instructions=instructions,
                    user=prompts.wrap_untrusted(job_text(job)), schema=ELIGIBILITY_SCHEMA,
                    job_id=job_id, max_tokens=1500)


# ------------------------------------------------------------------------------- evaluation
def decide(scores: dict, pay: str) -> str:
    """apply / maybe / skip — computed in code from rubric scores (plan.md §6)."""
    s = {d: scores[d]["score"] for d in DIMENSIONS}
    if pay == "below_min":
        return "skip"
    if (s["skills_match"] >= config.APPLY_MIN_SKILLS and s["seniority_fit"] >= config.APPLY_MIN_SENIORITY
            and s["red_flags"] <= config.APPLY_MAX_RED_FLAG):
        return "apply"
    if (s["skills_match"] >= config.MAYBE_MIN_SKILLS and s["seniority_fit"] >= config.MAYBE_MIN_SENIORITY
            and s["red_flags"] <= config.MAYBE_MAX_RED_FLAG):
        return "maybe"
    return "skip"


def shared_context(bank: factsmod.FactBank, prefs: dict | None = None) -> str:
    """The cached prefix block used by EVERY Sonnet stage (evaluate, tailor, drafts). It must be byte-identical
    across them — and across calls — so they all read one prompt-cache entry. Nothing per-job or per-request
    (timestamps, ids) may ever go in here."""
    prefs = prefs if prefs is not None else config.load_prefs()
    return (bank.for_prompt() + "\n\nCANDIDATE PREFERENCES\n"
            f"Target roles: {', '.join(prefs.get('target_roles', [])) or 'unspecified'}\n"
            f"Stack/seniority: {prefs.get('stack') or 'unspecified'}\n"
            "Candidate is based in India (IST).")


def extract_evaluation(llm: LLM, job, bank: factsmod.FactBank, prefs: dict, *, job_id: int | None = None) -> dict:
    _, instructions = prompts.load("evaluate")
    return llm.call(stage="evaluate", model=config.EVALUATE_MODEL, instructions=instructions,
                    context=shared_context(bank, prefs), user=prompts.wrap_untrusted(job_text(job)),
                    schema=EVALUATE_SCHEMA, job_id=job_id, max_tokens=8000)   # thinking tokens count too


def assess(llm: LLM, job, bank, prefs, profile) -> dict:
    """Full offline-pure assessment of a job dict/row (no DB writes). Used by the pipeline and `hub calibrate`.

    Returns {stage, decision, ...}: stage 'ineligible'|'needs_review'|'scored'.
    """
    text = job_text(job)
    ext = extract_eligibility(llm, job)
    elig = decide_eligibility(ext, text, prefs, profile)
    out = {"eligibility": {**elig, "extraction": ext}}
    if elig["decision"] == "ineligible":
        return {**out, "stage": "ineligible", "decision": "skip", "reason": "; ".join(elig["reasons"])}
    if elig["decision"] == "unclear":
        return {**out, "stage": "needs_review", "decision": "review", "reason": "; ".join(elig["reasons"])}
    ev = extract_evaluation(llm, job, bank, prefs)
    bad = [d for d in DIMENSIONS if not verify_quote(ev[d]["quote"], text)]
    out["evaluation"] = ev
    if bad:
        return {**out, "stage": "needs_review", "decision": "review",
                "reason": f"evaluation quotes not found in posting: {', '.join(bad)}"}
    pay = rules.pay_status(ext["pay"], ev["bucket"], profile)
    out["pay_status"] = pay
    sus = rules.pay_suspiciously_high(ext["pay"], prefs)
    if sus:
        return {**out, "stage": "needs_review", "decision": "review", "reason": sus.reason}
    decision = decide(ev, pay)
    return {**out, "stage": "scored", "decision": decision, "reason": f"{decision} (pay: {pay})"}


# ------------------------------------------------------------------------------- DB wrapper
def score_job(conn, llm: LLM, job, bank: factsmod.FactBank | None = None) -> str:
    """Eligibility -> evaluate for one job that already passed rules. Idempotent per description hash.
    Returns the resulting status (or 'error' leaving status unchanged)."""
    bank = bank or factsmod.load()
    prefs, profile = config.load_prefs(), config.load_yaml("profile.yaml")
    h = job["description_hash"]
    jid = job["id"]
    done = db.latest_score(conn, jid, "evaluate", h)
    if done is not None:  # crashed after scoring but before the transition: finish it, don't re-score
        if db.get_job(conn, jid)["status"] == "new":
            db.transition(conn, jid, "scored", f"{done['decision']} (recovered)")
        return db.get_job(conn, jid)["status"]
    ev_ver, el_ver = prompts.load("evaluate")[0], prompts.load("eligibility")[0]
    try:
        res = assess(llm, job, bank, prefs, profile)
    except PIIGuardError as e:
        db.add_score(conn, jid, "eligibility", {"error": str(e)}, "blocked", description_hash=h)
        db.transition(conn, jid, "needs_review", f"PII guard blocked the API request: {e}")
        return "needs_review"
    except SchemaError as e:
        db.transition(conn, jid, "needs_review", f"model response failed schema validation twice: {e}")
        return "needs_review"
    except LLMError as e:
        conn.execute("UPDATE jobs SET last_error=? WHERE id=?", (str(e)[:500], jid))
        conn.commit()
        log.warning("job %s: API failure, will retry next run: %s", jid, e)
        return "error"
    conn.execute("UPDATE jobs SET last_error=NULL WHERE id=?", (jid,))
    conn.commit()

    elig = res["eligibility"]
    db.add_score(conn, jid, "eligibility", elig, elig["decision"], model=config.ELIGIBILITY_MODEL,
                 prompt_version=el_ver, facts_version=bank.version, description_hash=h)
    if res["stage"] == "ineligible":
        db.transition(conn, jid, "ineligible", res["reason"])
        return "ineligible"
    if "evaluation" not in res:
        db.transition(conn, jid, "needs_review", res["reason"])
        return "needs_review"
    ev = res["evaluation"]
    db.add_score(conn, jid, "evaluate", {**ev, "pay_status": res.get("pay_status")}, res["decision"],
                 model=config.EVALUATE_MODEL, prompt_version=ev_ver, facts_version=bank.version, description_hash=h)
    conn.execute("UPDATE jobs SET bucket=? WHERE id=?", (ev["bucket"], jid))
    conn.commit()
    if res["stage"] == "needs_review":
        db.transition(conn, jid, "needs_review", res["reason"])
        return "needs_review"
    db.transition(conn, jid, "scored", res["reason"])
    return "scored"
