import pytest

from src import db, ingest
from tests.conftest import make_job


def job(**kw):
    j = make_job(**kw)
    j["description_hash"] = ingest.desc_hash(j["description"])
    return j


def test_html_to_text_handles_escaped_greenhouse_html_and_drops_scripts():
    raw = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    assert "Hello & welcome" in ingest.html_to_text(raw)
    out = ingest.html_to_text("<p>Hi</p><script>alert(1)</script><a href='http://x'>link</a>")
    assert "alert" not in out and "http://x" not in out and "link" in out


def test_city_aliases_and_company_canonical():
    assert ingest.normalize_location("Bengaluru, India") == ingest.normalize_location("Bangalore, India")
    assert ingest.canonical_company("Acme Technologies Pvt. Ltd.") == "acme"
    wl = [{"name": "Acme Corp", "canonical": "acme"}]
    assert ingest.canonical_company("Acme Corp", wl) == "acme"


def test_upsert_identity_and_unchanged_only_touches_last_seen(conn):
    jid, out = ingest.upsert_job(conn, job())
    assert out == "new"
    jid2, out2 = ingest.upsert_job(conn, job())
    assert (jid2, out2) == (jid, "unchanged")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0] == 1


def test_changed_description_reopens_scored_job(conn):
    jid, _ = ingest.upsert_job(conn, job())
    db.transition(conn, jid, "scored", "t")
    _, out = ingest.upsert_job(conn, job(description="Totally different text now."))
    assert out == "changed" and db.get_job(conn, jid)["status"] == "new"


def test_changed_description_keeps_shortlisted_status(conn):
    jid, _ = ingest.upsert_job(conn, job())
    for s in ("scored", "shortlisted"):
        db.transition(conn, jid, s, "t")
    ingest.upsert_job(conn, job(description="Edited text."))
    assert db.get_job(conn, jid)["status"] == "shortlisted"


def test_closure_after_two_missed_feeds(conn):
    a, _ = ingest.upsert_job(conn, job(source="greenhouse:x", external_id="a"))
    b, _ = ingest.upsert_job(conn, job(source="greenhouse:x", external_id="b"))
    ingest.mark_missing(conn, "greenhouse:x", {"b"})
    assert db.get_job(conn, a)["status"] == "new"
    ingest.mark_missing(conn, "greenhouse:x", {"b"})
    assert db.get_job(conn, a)["status"] == "closed" and db.get_job(conn, b)["status"] == "new"


def test_reappearing_job_resets_missed_counter(conn):
    a, _ = ingest.upsert_job(conn, job(source="greenhouse:x", external_id="a"))
    ingest.mark_missing(conn, "greenhouse:x", set())
    ingest.upsert_job(conn, job(source="greenhouse:x", external_id="a"))
    ingest.mark_missing(conn, "greenhouse:x", set())
    assert db.get_job(conn, a)["status"] == "new"


def test_cross_source_duplicates_marked_not_deleted(conn):
    desc = "Build Python services for our platform team. " * 10
    a, _ = ingest.upsert_job(conn, job(source="greenhouse:x", external_id="1", title="Backend Engineer",
                                       location_norm="bangalore", description=desc))
    b, _ = ingest.upsert_job(conn, job(source="remotive", external_id="9", title="Backend  Engineer!",
                                       location_norm="bangalore", description=desc))
    c, _ = ingest.upsert_job(conn, job(source="remotive", external_id="10", title="Backend Engineer",
                                       location_norm="bangalore", description="Completely unrelated role about sales. " * 10))
    ingest.mark_duplicates(conn)
    assert db.get_job(conn, b)["dup_of"] == a
    assert db.get_job(conn, c)["dup_of"] is None
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_failing_source_is_recorded_not_raised(conn):
    def boom():
        raise RuntimeError("endpoint moved")
    res = ingest.run_source(conn, "greenhouse:gone", boom)
    assert res["error"] and "endpoint moved" in res["error"]
    assert conn.execute("SELECT errors FROM runs").fetchone()[0]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "http://localhost/admin",
                                 "http://127.0.0.1:8765/", "http://169.254.169.254/latest/meta-data",
                                 "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/", "http:///nohost",
                                 "javascript:alert(1)"])
def test_unsafe_urls_rejected(url):
    with pytest.raises(ingest.UnsafeURL):
        ingest.check_url_safe(url)


def test_json_ld_job_posting_parsed():
    body = ('<html><title>x</title><script type="application/ld+json">{"@type":"JobPosting","title":"Data Engineer",'
            '"hiringOrganization":{"name":"Globex"},"description":"<p>Build pipelines</p>"}</script></html>')
    p = ingest.parse_posting_page("https://globex.example/jobs/1", body)
    assert p["title"] == "Data Engineer" and p["company"] == "Globex"
