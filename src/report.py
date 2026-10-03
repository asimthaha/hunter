"""Per-job markdown report in reports/. Posting text and model-written text go in fenced blocks so
nothing from a posting is rendered as markdown/links (plan.md §12)."""
from __future__ import annotations

import re

from . import config, db
from .drafts import answer_yourself


def fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    tick = "`" * max(3, longest + 1)
    return f"{tick}text\n{text or ''}\n{tick}"


def build(conn, job_id: int) -> str:
    job = db.get_job(conn, job_id)
    h = job["description_hash"]
    lines = [f"# {job['title']} — {job['company']}", "",
             f"- Job ID: {job_id}", f"- Status: {job['status']} ({job['status_reason'] or ''})",
             f"- Bucket: {job['bucket'] or 'unknown'}", f"- Source: {job['source']}",
             f"- Location: {job['location_raw']}", f"- URL: {job['url']}", ""]
    rules = db.score_json(db.latest_score(conn, job_id, "rules", h))
    if rules.get("flags"):
        lines += ["## Rule flags", ""]
        lines += [f"- **{f['kind']}** ({f['action']}): {f['reason']}" for f in rules["flags"]]
        lines += ["", fence("\n".join(f["quote"] for f in rules["flags"] if f["quote"])), ""]
    elig = db.score_json(db.latest_score(conn, job_id, "eligibility", h))
    if elig:
        lines += ["## Eligibility", "", f"Decision: **{elig.get('decision')}**", ""]
        lines += [f"- {r}" for r in elig.get("reasons", [])]
        ext = elig.get("extraction") or {}
        if ext:
            pay = ext.get("pay", {})
            lines += ["", f"- Pay: {'stated' if pay.get('stated') else 'not stated'} "
                          f"{pay.get('amount_min')}–{pay.get('amount_max')} {pay.get('currency')} / {pay.get('period')}"]
            lines += [f"- Regions: {ext.get('allowed_regions', {}).get('value')}",
                      f"- Work authorization: {ext.get('work_authorization', {}).get('value')}",
                      f"- Employment type: {ext.get('employment_type', {}).get('value')}"]
        lines.append("")
    ev = db.score_json(db.latest_score(conn, job_id, "evaluate", h))
    if ev:
        row = db.latest_score(conn, job_id, "evaluate", h)
        lines += ["## Evaluation", "", f"Decision: **{row['decision']}**  (pay: {ev.get('pay_status')}; "
                  f"model {row['model']}, prompt v{row['prompt_version']}, facts v{row['facts_version']})", ""]
        for d in ("skills_match", "seniority_fit", "domain", "timezone_overlap", "red_flags"):
            if d in ev:
                lines += [f"### {d} — {ev[d]['score']}/5", fence(f"{ev[d]['reason']}\n\nQuote: {ev[d]['quote']}"), ""]
        lines += ["Summary:", fence(ev.get("summary", "")), ""]
    tl = db.score_json(db.latest_score(conn, job_id, "tailor", h))
    if tl:
        lines += ["## Resume", ""]
        if tl.get("errors"):
            lines += ["**Not generated — validation failed:**"] + [f"- {e}" for e in tl["errors"]]
        elif tl.get("render"):
            r = tl["render"]
            lines += [f"PDF: {r['path']} ({r['pages']} page(s); dropped for fit: {r['dropped'] or 'none'})"]
            if r.get("parse_problems"):
                lines += ["", "Text missing from PDF extraction:"] + [f"- {p}" for p in r["parse_problems"]]
        me = tl.get("selection", {}).get("missing_evidence", [])
        if me:
            lines += ["", "Missing evidence (suggestions — you write any fact yourself):"]
            lines += [f"- {m['suggestion']}" for m in me]
        lines.append("")
    dr = db.score_json(db.latest_score(conn, job_id, "drafts", h))
    if dr:
        lines += ["## Drafts (grounded; edit before use)", ""]
        for d in dr.get("drafts", []):
            lines += [f"### {d['kind']}{': ' + d['question'] if d.get('question') else ''}"]
            lines += [fence(d["text"])] if d.get("text") else [f"Rejected by validator: {d.get('errors')}"]
        if dr.get("answer_yourself_questions"):
            lines += ["", "Answer yourself:"] + [f"- {q}" for q in dr["answer_yourself_questions"]]
        lines.append("")
    lines += ["## Answer yourself (never auto-answered)", ""]
    lines += [f"- {k}: {v}" for k, v in answer_yourself(config.load_yaml("profile.yaml")).items()]
    lines += ["", "## Posting (untrusted text)", "", fence(job["description"]), ""]
    text = "\n".join(lines)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / f"{job_id}.md"
    path.write_text(text, encoding="utf-8")
    db.add_artifact(conn, job_id, "report", path)
    return text
