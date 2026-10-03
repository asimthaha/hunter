import pytest

from src import actions, db
from tests.conftest import make_job


def test_new_job_starts_new_and_logs_event(conn):
    jid = db.insert_job(conn, make_job())
    assert db.get_job(conn, jid)["status"] == "new"
    assert conn.execute("SELECT COUNT(*) FROM job_events WHERE job_id=?", (jid,)).fetchone()[0] == 1


def test_valid_path_to_offer(conn):
    jid = db.insert_job(conn, make_job())
    for s in ("scored", "shortlisted", "tailored", "applied", "interviewing", "offer"):
        db.transition(conn, jid, s, "test")
    assert [e["to_status"] for e in conn.execute("SELECT * FROM job_events ORDER BY id")][-1] == "offer"


@pytest.mark.parametrize("path", [
    ["applied"],                                  # can't skip ahead
    ["scored", "applied"],
    ["scored", "tailored"],
    ["filtered_out", "scored"],
    ["scored", "skipped", "shortlisted"],
])
def test_invalid_transitions_rejected(conn, path):
    jid = db.insert_job(conn, make_job())
    with pytest.raises(db.InvalidTransition):
        for s in path:
            db.transition(conn, jid, s, "test")


def test_transition_requires_reason(conn):
    jid = db.insert_job(conn, make_job())
    with pytest.raises(ValueError):
        db.transition(conn, jid, "scored", "")


def test_closed_only_from_pre_applied(conn):
    jid = db.insert_job(conn, make_job())
    for s in ("scored", "shortlisted", "tailored", "applied"):
        db.transition(conn, jid, s, "t")
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, jid, "closed", "gone")


def test_unique_source_external_id(conn):
    import sqlite3
    db.insert_job(conn, make_job())
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_job(conn, make_job())


def test_wal_mode(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_review_from_needs_review_goes_through_scored(conn):
    jid = db.insert_job(conn, make_job())
    db.transition(conn, jid, "needs_review", "unclear")
    actions.review(conn, jid, "shortlist")
    tos = [e["to_status"] for e in conn.execute("SELECT * FROM job_events ORDER BY id")]
    assert tos[-2:] == ["scored", "shortlisted"]


def test_applied_sets_follow_up(conn):
    jid = db.insert_job(conn, make_job())
    for s in ("scored", "shortlisted", "tailored"):
        db.transition(conn, jid, s, "t")
    follow = actions.mark_applied(conn, jid, 5)
    assert db.get_job(conn, jid)["follow_up_on"] == follow
