"""Grounded free-text drafts (plan.md §9): referral notes and screening answers.

Every sentence must cite fact IDs and pass validate.validate_draft. Visa / salary / CTC /
notice-period / legal questions are NEVER sent to the model — they are shown as 'answer yourself'.
"""
from __future__ import annotations

import re

from . import config, db, facts as factsmod, prompts, validate
from .evaluate import job_text, shared_context
from .llm import LLM, LLMError, PIIGuardError, SchemaError

SENSITIVE_Q = re.compile(
    r"\b(visa|sponsor\w*|work authori[sz]ation|authori[sz]ed to work|salary|ctc|compensation|pay expectation|"
    r"expected pay|current pay|notice|joining date|how soon|legal|criminal|background check|citizen\w*|"
    r"right to work|relocat\w*)\b", re.I)

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"drafts": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["referral_note", "screening_answer"]},
        "question": {"type": "string"},
        "declined": {"type": "boolean"},
        "sentences": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "fact_ids"]}},
    }, "required": ["kind", "question", "declined", "sentences"]}}},
    "required": ["drafts"],
}


def answer_yourself(profile: dict) -> dict:
    """Values for the questions we never auto-answer, filled in locally from profile.yaml."""
    def money(x):
        return f"{x['amount']:,} {x['currency']} / {x.get('period', 'year')}" if isinstance(x, dict) and x.get("amount") else "—"
    notice = profile.get("notice_days")
    return {
        "Visa / work authorization": profile.get("work_authorization", "— (answer yourself)"),
        "Current CTC": money(profile.get("current_ctc")),
        "Expected CTC": money(profile.get("expected_ctc")),
        "Notice period": ("immediate" if notice == 0 else f"{notice} days") if notice is not None else "—",
    }


def generate(conn, llm: LLM, job_id: int, questions: list[str] | None = None) -> dict:
    job = db.get_job(conn, job_id)
    bank = factsmod.load()
    questions = questions or []
    blocked = [q for q in questions if SENSITIVE_Q.search(q)]
    allowed = [q for q in questions if q not in blocked]
    version, instructions = prompts.load("drafts")
    ask = "Write a referral note for this job."
    if allowed:
        ask += " Also answer these screening questions, one draft each:\n" + "\n".join(f"- {q}" for q in allowed)
    try:
        out = llm.call(stage="drafts", model=config.DRAFT_MODEL, instructions=instructions,
                       context=shared_context(bank), user=ask + "\n\n" + prompts.wrap_untrusted(job_text(job)),
                       schema=DRAFT_SCHEMA, job_id=job_id, max_tokens=8000)
    except (PIIGuardError, SchemaError, LLMError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "drafts": [], "answer_yourself_questions": blocked}
    ctx = [job["company"] or "", job["title"] or ""]
    drafts = []
    for d in out["drafts"]:
        if d["declined"]:
            drafts.append({**d, "text": "", "errors": ["model declined"]})
            continue
        errors = validate.validate_draft(d["sentences"], bank, ctx)
        drafts.append({"kind": d["kind"], "question": d["question"], "errors": errors,
                       "text": "" if errors else " ".join(s["text"].strip() for s in d["sentences"]),
                       "fact_ids": sorted({i for s in d["sentences"] for i in s["fact_ids"]})})
    result = {"drafts": drafts, "answer_yourself_questions": blocked}
    db.add_score(conn, job_id, "drafts", result, "ok", model=config.DRAFT_MODEL, prompt_version=version,
                 facts_version=bank.version, description_hash=job["description_hash"])
    for d in drafts:
        if d["kind"] == "referral_note" and d["text"]:
            db.add_approval(conn, str(job_id), "contact_sent", {"job_id": job_id, "note": d["text"]})
    return {"ok": True, **result}
