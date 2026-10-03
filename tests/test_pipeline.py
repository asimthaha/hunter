import pytest

from src import actions, config, db, evaluate, pipeline
from src.llm import LLMError, SchemaError
from tests.conftest import FakeLLM, eligibility_ok, evaluation, make_job

DESC = "We need a backend engineer. Python and FastAPI. Remote from India. Pay 30 LPA."


def add(conn, **kw):
    return db.insert_job(conn, make_job(description=DESC, **kw))


def llm(elig=None, ev=None):
    return FakeLLM({"eligibility": elig or eligibility_ok(), "evaluate": ev or evaluation()})


def test_happy_path_scores_and_apply_decision(conn):
    jid = add(conn)
    assert pipeline.process_job(conn, llm(), jid) == "scored"
    row = db.latest_score(conn, jid, "evaluate")
    assert row["decision"] == "apply" and row["model"] == config.EVALUATE_MODEL
    assert row["prompt_version"] and row["facts_version"] == "1"
    assert db.get_job(conn, jid)["bucket"] == "india"
    assert (config.REPORTS_DIR / f"{jid}.md").exists()


def test_idempotent_rerun_does_not_rescore(conn):
    jid = add(conn)
    fake = llm()
    pipeline.process_job(conn, fake, jid)
    pipeline.process_job(conn, fake, jid)
    pipeline.process_new(conn, fake)
    assert fake.calls.count("evaluate") == 1
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE stage='evaluate'").fetchone()[0] == 1


def test_rule_rejection_records_reason_and_skips_llm(conn):
    jid = db.insert_job(conn, make_job(description="Pay a registration fee to join. Python."))
    fake = llm()
    assert pipeline.process_job(conn, fake, jid) == "filtered_out"
    assert "asks_for_money" in db.get_job(conn, jid)["status_reason"]
    assert fake.calls == []


def test_unverified_eligibility_quote_goes_to_needs_review(conn):
    jid = add(conn)
    elig = eligibility_ok(allowed_regions={"value": ["india"], "quote": "India only, no exceptions"})
    assert pipeline.process_job(conn, llm(elig), jid) == "needs_review"
    assert "quote not found" in db.get_job(conn, jid)["status_reason"]


def test_verified_quote_is_accepted_whitespace_insensitive(conn):
    jid = add(conn)
    elig = eligibility_ok(allowed_regions={"value": ["india"], "quote": "Remote   from\nIndia"})
    assert pipeline.process_job(conn, llm(elig), jid) == "scored"


def test_ineligible_region_lock(conn):
    jid = add(conn)
    elig = eligibility_ok(allowed_regions={"value": ["us"], "quote": "Remote from India"})
    assert pipeline.process_job(conn, llm(elig), jid) == "ineligible"


def test_model_flagged_money_request_is_ineligible(conn):
    jid = add(conn)
    elig = eligibility_ok(asks_for_money={"value": True, "quote": "Python and FastAPI"})
    assert pipeline.process_job(conn, llm(elig), jid) == "ineligible"


def test_below_min_pay_is_skip_not_apply(conn):
    jid = add(conn)
    elig = eligibility_ok(pay={"stated": True, "amount_min": 20, "amount_max": 22, "currency": "INR",
                               "period": "lakh_per_annum", "quote": "Pay 30 LPA"})
    assert pipeline.process_job(conn, llm(elig), jid) == "scored"
    assert db.latest_score(conn, jid, "evaluate")["decision"] == "skip"


def test_pay_not_stated_does_not_block_apply(conn):
    jid = add(conn)
    pipeline.process_job(conn, llm(), jid)
    assert db.score_json(db.latest_score(conn, jid, "evaluate"))["pay_status"] == "not_stated"
    assert db.latest_score(conn, jid, "evaluate")["decision"] == "apply"


def test_unverified_evaluation_quote_needs_review(conn):
    jid = add(conn)
    assert pipeline.process_job(conn, llm(ev=evaluation(quote="never in posting")), jid) == "needs_review"


def test_api_failure_leaves_state_and_retries_next_run(conn):
    jid = add(conn)

    def boom(kw):
        raise LLMError("overloaded")
    assert pipeline.process_job(conn, FakeLLM({"eligibility": boom}), jid) == "error"
    assert db.get_job(conn, jid)["status"] == "new" and "overloaded" in db.get_job(conn, jid)["last_error"]
    assert pipeline.process_job(conn, llm(), jid) == "scored"
    assert db.get_job(conn, jid)["last_error"] is None


def test_schema_failure_goes_to_needs_review(conn):
    jid = add(conn)

    def bad(kw):
        raise SchemaError("twice")
    assert pipeline.process_job(conn, FakeLLM({"eligibility": bad}), jid) == "needs_review"


def test_duplicates_are_not_scored(conn):
    a = add(conn)
    b = add(conn, external_id="2")
    conn.execute("UPDATE jobs SET dup_of=? WHERE id=?", (a, b))
    conn.commit()
    pipeline.process_new(conn, llm())
    assert db.get_job(conn, b)["status"] == "new"


@pytest.mark.parametrize("scores,pay,expected", [
    ((5, 4, 4, 5, 1), "ok", "apply"),
    ((4, 3, 1, 1, 1), "not_stated", "apply"),
    ((5, 4, 4, 5, 2), "ok", "maybe"),        # red flag 2 -> not apply
    ((3, 2, 3, 3, 2), "ok", "maybe"),
    ((2, 4, 4, 5, 1), "ok", "skip"),
    ((5, 4, 4, 5, 1), "below_min", "skip"),
])
def test_decision_rule(scores, pay, expected):
    assert evaluate.decide(evaluation(*scores), pay) == expected


def test_digest_lists_new_apply_and_flags_silent_source(conn):
    from src import digest
    jid = add(conn)
    pipeline.process_job(conn, llm(), jid)
    for _ in range(2):
        conn.execute("INSERT INTO runs (started_at, finished_at, source, jobs_fetched, jobs_new) VALUES ('a','b','greenhouse:x',0,0)")
    conn.commit()
    text = digest.build(conn)
    assert f"[{jid}]" in text and "0 jobs for 2 consecutive runs" in text
