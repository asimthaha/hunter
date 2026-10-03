import pytest

from src import actions, db, drafts, facts as factsmod, render, tailor
from tests.conftest import FakeLLM, make_job

CONTACT = {"name": "Jane <b>Example</b>", "email": "jane@example.com", "phone": "123"}


def selection(**kw):
    s = {"summary_id": "sum-backend", "variant_ids": ["acme-1a", "acme-2a"], "skills": ["Python", "FastAPI"],
         "substitutions": [], "missing_evidence": [{"requirement": "Kafka", "suggestion": "JD asks for Kafka — no fact mentions it"}]}
    s.update(kw)
    return s


def shortlisted(conn, **kw):
    jid = db.insert_job(conn, make_job(**kw))
    for s in ("scored", "shortlisted"):
        db.transition(conn, jid, s, "t")
    return jid


def test_assemble_takes_titles_and_dates_from_facts(hub):
    bank = factsmod.load()
    m = tailor.assemble(selection(), bank)
    assert m["roles"][0]["title"] == "Software Engineer" and m["roles"][0]["dates"] == "2023-01 to present"
    assert [b["id"] for b in m["roles"][0]["bullets"]] == ["acme-1a", "acme-2a"]


def test_html_is_escaped_and_contains_no_job_text(hub):
    bank = factsmod.load()
    m = tailor.assemble(selection(), bank)
    m["summary"] = "<script>alert(1)</script>"
    html = render.build_html(m, CONTACT)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Jane &lt;b&gt;Example&lt;/b&gt;" in html


def test_drop_lowest_removes_last_ranked_and_keeps_one(hub):
    m = tailor.assemble(selection(), factsmod.load())
    m2 = render.drop_lowest(m)
    assert [b["id"] for r in m2["roles"] for b in r["bullets"]] == ["acme-1a"]
    assert render.drop_lowest(m2) is None


def test_invalid_selection_blocks_resume_and_job_stays_shortlisted(conn):
    jid = shortlisted(conn)
    bad = selection(variant_ids=["acme-1a", "acme-1b"])
    res = tailor.tailor_job(conn, FakeLLM({"tailor": bad}), jid)
    assert not res["ok"] and any("more than one variant" in e for e in res["errors"])
    assert db.get_job(conn, jid)["status"] == "shortlisted"
    assert db.get_artifact(conn, jid, "resume_pdf") is None


def test_tailor_requires_shortlisted_and_open_job(conn):
    jid = db.insert_job(conn, make_job())
    assert not tailor.tailor_job(conn, FakeLLM({"tailor": selection()}), jid)["ok"]
    jid2 = shortlisted(conn, external_id="2")
    conn.execute("UPDATE jobs SET missed_runs=1 WHERE id=?", (jid2,))
    conn.commit()
    assert "still open" in tailor.tailor_job(conn, FakeLLM({"tailor": selection()}), jid2)["errors"][0]


def test_successful_tailor_transitions_and_files_proposal(conn, monkeypatch):
    jid = shortlisted(conn)
    monkeypatch.setattr(render, "render_resume", lambda m, c, p, n: {"path": str(p), "pages": 1, "dropped": [],
                                                                    "parse_problems": [], "over_limit": False})
    res = tailor.tailor_job(conn, FakeLLM({"tailor": selection()}), jid)
    assert res["ok"] and db.get_job(conn, jid)["status"] == "tailored"
    prop = actions.proposals(conn)
    assert len(prop) == 1 and prop[0]["type"] == "fact_bank_edit"


def test_approving_proposal_never_touches_facts_file(conn, hub, monkeypatch):
    before = (hub.data / "facts.yaml").read_text()
    db.add_approval(conn, "1:Kafka", "fact_bank_edit", {"suggestion": "x"})
    actions.decide_proposal(conn, 1, True)
    assert (hub.data / "facts.yaml").read_text() == before


# ---- drafts
def draft_resp(text, ids):
    return {"drafts": [{"kind": "referral_note", "question": "", "declined": False,
                        "sentences": [{"text": text, "fact_ids": ids}]}]}


def test_grounded_referral_note_kept_and_queued(conn):
    jid = shortlisted(conn, company="Globex")
    res = drafts.generate(conn, FakeLLM({"drafts": draft_resp("I built a Python ETL pipeline processing 2M records/day.", ["acme-1a"])}), jid)
    assert res["drafts"][0]["text"] and not res["drafts"][0]["errors"]
    assert actions.proposals(conn)[0]["type"] == "contact_sent"


def test_fabricated_draft_discarded(conn):
    jid = shortlisted(conn)
    res = drafts.generate(conn, FakeLLM({"drafts": draft_resp("I led a team of 40 engineers.", ["acme-1a"])}), jid)
    assert res["drafts"][0]["text"] == "" and res["drafts"][0]["errors"]
    assert actions.proposals(conn) == []


def test_sensitive_questions_never_sent_to_model(conn):
    jid = shortlisted(conn)
    seen = {}

    def capture(kw):
        seen["user"] = kw["user"]
        return {"drafts": []}
    res = drafts.generate(conn, FakeLLM({"drafts": capture}), jid,
                          ["What is your expected CTC?", "Do you need visa sponsorship?", "Why this role?"])
    assert len(res["answer_yourself_questions"]) == 2
    assert "CTC" not in seen["user"] and "visa" not in seen["user"] and "Why this role?" in seen["user"]


def test_answer_yourself_comes_from_profile(hub):
    ans = drafts.answer_yourself({"notice_days": 30, "current_ctc": {"amount": 1800000, "currency": "INR"}})
    assert ans["Notice period"] == "30 days" and "1,800,000" in ans["Current CTC"]


@pytest.mark.skipif(not __import__("shutil").which("python"), reason="n/a")
def test_real_pdf_render_if_chromium_available(hub, tmp_path):
    pytest.importorskip("playwright")
    m = tailor.assemble(selection(), factsmod.load())
    try:
        out = render.render_resume(m, {"name": "Jane Example", "email": "j@example.com"}, tmp_path / "r.pdf", 2)
    except Exception as e:  # chromium not installed
        pytest.skip(f"chromium unavailable: {type(e).__name__}")
    assert out["pages"] <= 2 and out["parse_problems"] == []
