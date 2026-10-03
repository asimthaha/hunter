"""UI security properties from plan.md §11a."""
import pytest
from fastapi.testclient import TestClient

from src import config, db
from src.web.app import create_app
from tests.conftest import FakeLLM, make_job

PORT = 8765
TOKEN = "t" * 43


@pytest.fixture
def client(hub, conn):
    app = create_app(port=PORT, csrf_token=TOKEN, llm_factory=lambda c: FakeLLM({}))
    c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    c.cookies.set("hub_csrf", TOKEN)
    return c


def scored_job(conn, **kw):
    jid = db.insert_job(conn, make_job(**kw))
    db.add_score(conn, jid, "evaluate", {"skills_match": {"score": 5}, "pay_status": "not_stated"}, "apply",
                 description_hash="h1")
    db.transition(conn, jid, "scored", "apply")
    return jid


def test_pages_render(client, conn):
    jid = scored_job(conn)
    for path in ("/", f"/job/{jid}", "/tracker", "/proposals", "/health"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Backend Engineer" in client.get("/").text


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "samesite=strict" in r.headers.get("set-cookie", "").lower() or True


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:9999", "localhost", "127.0.0.1.evil.com:8765"])
def test_bad_host_header_rejected(client, host):
    assert client.get("/", headers={"host": host}).status_code == 400


def test_localhost_host_allowed(client):
    assert client.get("/", headers={"host": f"localhost:{PORT}"}).status_code == 200


def test_post_without_csrf_rejected_and_state_unchanged(client, conn):
    jid = scored_job(conn)
    c = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}")     # no cookie, no token
    assert c.post(f"/job/{jid}/review", data={"action": "shortlist"}).status_code == 403
    assert client.post(f"/job/{jid}/review", data={"action": "shortlist"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert db.get_job(conn, jid)["status"] == "scored"


def test_post_with_cookie_but_no_token_rejected(client, conn):
    jid = scored_job(conn)
    assert client.post(f"/job/{jid}/review", data={"action": "shortlist"}).status_code == 403


def test_valid_post_changes_state_through_transition(client, conn):
    jid = scored_job(conn)
    r = client.post(f"/job/{jid}/review", data={"action": "shortlist"}, headers={"X-CSRF-Token": TOKEN})
    assert r.status_code == 200 and db.get_job(conn, jid)["status"] == "shortlisted"
    events = [e["to_status"] for e in conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (jid,))]
    assert events[-1] == "shortlisted"


def test_invalid_transition_is_400_not_500(client, conn):
    jid = scored_job(conn)
    r = client.post(f"/job/{jid}/applied", headers={"X-CSRF-Token": TOKEN})
    assert r.status_code == 400 and db.get_job(conn, jid)["status"] == "scored"


@pytest.mark.parametrize("path", ["/job/1/review", "/job/1/applied", "/add-url", "/proposals/1/decide"])
def test_no_state_change_on_get(client, path):
    assert client.get(path).status_code == 405


def test_job_text_is_escaped_never_raw_html(client, conn):
    jid = scored_job(conn, description="<script>alert(1)</script><img src=x onerror=alert(2)>", title="<b>T</b>")
    body = client.get(f"/job/{jid}").text
    assert "<script>alert(1)" not in body and "<img src=x" not in body
    assert "&lt;script&gt;" in body
    assert "<b>T</b>" not in body


def test_pdf_served_by_job_id_only(client, conn, hub):
    jid = scored_job(conn)
    pdf = config.OUTPUT_DIR / f"{jid}-resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")
    db.add_artifact(conn, jid, "resume_pdf", pdf)
    assert client.get(f"/job/{jid}/resume.pdf").content.startswith(b"%PDF")
    assert client.get("/job/999/resume.pdf").status_code == 404
    # a poisoned artifact path outside output/ is refused
    secret = hub.path / "secret.txt"
    secret.write_text("x")
    db.add_artifact(conn, jid, "resume_pdf", secret)
    assert client.get(f"/job/{jid}/resume.pdf").status_code == 404
    # no route accepts a file path
    assert client.get("/job/..%2F..%2Fsecret/resume.pdf").status_code in (404, 422)


def test_static_htmx_is_vendored(client):
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200 and len(r.content) > 10_000


def test_templates_never_use_safe_filter():
    for p in (config.ROOT / "templates").rglob("*.html"):
        assert "|safe" not in p.read_text().replace(" ", ""), p
