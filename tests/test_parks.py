"""Infopark (HTML) and Technopark (Inertia JSON + data-page) sources, with the network mocked."""
import html
import json
from types import SimpleNamespace

from src import db, ingest, rules


def ip_row(date, title, company, last, cid, jid):
    return (f'<tr>\n<td class="head">{date}</td>\n<td class="head">{title}</td>\n<td class="date">{company}</td>\n'
            f'<td>{last}</td>\n<td class="btn-sec">\n<a href="https://infopark.in/company-jobs/details/{cid}/{jid}">'
            f'<button>Details</button></a>\n</td>\n</tr>\n')


def ip_list(*rows):
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


IP_DETAIL = """<section><div class="carer-box"><div class="logo-con"><div class="con">
<h4>Experion Technologies</h4><span>04843067309</span><br><span>careers@experionglobal.com</span></div></div></div>
<div class="comp-job-deatiil"><div class="deatil-box"><h4>React Developer</h4>
Skills &ndash; React, REST APIs<br />Work Location - Kochi/Trivandrum<br />
&bull; Build <b>UI</b> components<script>alert(1)</script><br />
</div></div></section><footer>footer text</footer>"""


class Fake:
    """Routes URL -> response text/json and records what was requested."""
    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def get(self, url, **kw):
        self.requested.append(url)
        return SimpleNamespace(text=self.pages[url])

    def get_json(self, url, **kw):
        self.requested.append(f"{url}?page={kw['params']['page']}")
        return self.pages[(url, kw["params"]["page"])]


def test_infopark_row_and_detail_parsing():
    rows = ingest.parse_infopark_list(ip_list(ip_row("03-10-2026", "Meta Ads &amp; SEO", "Mozilor Pvt. Ltd.", "04 Oct 2026", 333, 25893)))
    assert rows == [{"posted": "2026-10-03", "title": "Meta Ads & SEO", "company": "Mozilor Pvt. Ltd.",
                     "last_date": "04 Oct 2026", "url": "https://infopark.in/company-jobs/details/333/25893", "job_id": "25893"}]
    d = ingest.parse_infopark_detail(IP_DETAIL)
    assert d["company"] == "Experion Technologies" and "careers@experionglobal.com" in d["contacts"]
    text = ingest.html_to_text(d["description_html"])
    assert "React Developer" in text and "alert" not in text and "footer text" not in text


def test_fetch_infopark_paginates_until_empty_and_skips_known_details(monkeypatch):
    base = "https://infopark.in/companies-job"
    fake = Fake({
        base: ip_list(ip_row("03-10-2026", "React Developer", "Experion Technologies", "30 Oct 2026", 103, 1),
                      ip_row("03-10-2026", "Old Known Job", "Experion Technologies", "30 Oct 2026", 103, 2)),
        base + "?page=2": ip_list(ip_row("02-10-2026", "QA Engineer", "Other Co", "30 Oct 2026", 5, 3)),
        base + "?page=3": ip_list(),
        "https://infopark.in/company-jobs/details/103/1": IP_DETAIL,
        "https://infopark.in/company-jobs/details/5/3": IP_DETAIL,
    })
    monkeypatch.setattr(ingest, "_get", fake.get)
    monkeypatch.setattr(ingest, "_polite", lambda: None)
    jobs = ingest.fetch_infopark([], known={"2": ("cached description", "Infopark, Kerala")})
    assert [j["external_id"] for j in jobs] == ["1", "2", "3"]
    assert all(j["source"] == "infopark" for j in jobs)
    assert jobs[0]["location_raw"] == "Kochi/Trivandrum" and "careers@experionglobal.com" in jobs[0]["description"]
    assert jobs[1]["description"] == "cached description"
    assert "https://infopark.in/company-jobs/details/103/2" not in fake.requested   # known job: no detail request
    assert fake.requested.count(base + "?page=3") == 1                              # stops at first empty page


def test_infopark_detail_failure_skips_only_that_job(monkeypatch):
    base = "https://infopark.in/companies-job"
    pages = {base: ip_list(ip_row("03-10-2026", "A", "X", "d", 1, 1), ip_row("03-10-2026", "B", "Y", "d", 2, 2)),
             base + "?page=2": ip_list(), "https://infopark.in/company-jobs/details/2/2": IP_DETAIL}

    def get(url, **kw):
        if url.endswith("/1/1"):
            raise RuntimeError("boom")
        return SimpleNamespace(text=pages[url])
    monkeypatch.setattr(ingest, "_get", get)
    monkeypatch.setattr(ingest, "_polite", lambda: None)
    assert [j["external_id"] for j in ingest.fetch_infopark([])] == ["2"]


def tp_detail(jl):
    return '<div id="app" data-page="' + html.escape(json.dumps({"component": "JobListing/JobDetailPage",
                                                                  "props": {"jobListing": jl}})) + '"></div>'


def test_fetch_technopark_uses_json_list_and_detail_page(monkeypatch):
    api = "https://technopark.in/api/paginated-jobs"
    job = {"id": 33329, "job_title": "QA Intern", "posted_date": "2026-10-02", "company": {"company": "SubHosting Innovations (P) Ltd"}}
    walk = {"id": 33321, "job_title": "Management Trainee", "posted_date": "2026-10-01", "company": {"company": "Scalevel"}}
    fake = Fake({
        (api, 1): {"current_page": 1, "last_page": 2, "data": [job]},
        (api, 2): {"current_page": 2, "last_page": 2, "data": [walk]},
        "https://technopark.in/job-details/33329": tp_detail({
            "job_description": "<p>Test <strong>web apps</strong></p>", "preferred_skills": "<p>BCA</p>",
            "contact_email": "careers@subhosting.net", "closing_date": "2026-10-18", "is_walk_in": 0}),
        "https://technopark.in/job-details/33321": tp_detail({
            "job_description": "<p>Trainee role</p>", "contact_email": "hr@scalevel.in", "closing_date": "2026-10-05",
            "is_walk_in": 1, "walk_in_start_date": "2026-10-05", "walk_in_start_time": "11:30:00",
            "walk_in_end_time": "15:00:00", "walk_in_address": "<p>Walk in at <b>Tower B</b></p>"}),
    })
    monkeypatch.setattr(ingest, "_get", fake.get)
    monkeypatch.setattr(ingest, "_get_json", fake.get_json)
    monkeypatch.setattr(ingest, "_polite", lambda: None)
    jobs = ingest.fetch_technopark([])
    assert [j["external_id"] for j in jobs] == ["33329", "33321"]
    a, b = jobs
    assert a["location_raw"] == "Technopark, Thiruvananthapuram, Kerala" and a["url"].endswith("/job-details/33329")
    assert "web apps" in a["description"] and "<strong>" not in a["description"] and "careers@subhosting.net" in a["description"]
    assert "Walk-in drive on 2026-10-05 11:30:00-15:00:00" in b["description"] and "Tower B" in b["description"]


def test_priority_sources_run_first_and_close_missing_jobs(conn, monkeypatch):
    order = []

    def fake_infopark(wl, known):
        order.append("infopark")
        return [ingest.make_job(source="infopark", external_id="1", company="X", title="Dev", location="Kochi",
                                url="u", description_text="d")]

    def fake_techno(wl, known):
        order.append("technopark")
        return []
    monkeypatch.setattr(ingest, "PRIORITY_FETCHERS", {"infopark": fake_infopark, "technopark": fake_techno})
    monkeypatch.setattr(ingest, "load_watchlist", lambda: [])        # no real network in tests
    for name in ("fetch_remotive", "fetch_himalayas"):
        monkeypatch.setattr(ingest, name, lambda wl, n=name: order.append(n) or [])
    ingest.run_all(conn)
    assert order[:2] == ["infopark", "technopark"] and "fetch_remotive" in order
    # job gone from the (complete) feed on two consecutive runs -> closed
    fake_infopark_gone = lambda wl, known: []
    monkeypatch.setitem(ingest.PRIORITY_FETCHERS, "infopark", fake_infopark_gone)
    ingest.run_all(conn)
    ingest.run_all(conn)
    assert db.get_job(conn, 1)["status"] == "closed"


def test_known_jobs_reuses_stored_description(conn):
    jid, _ = ingest.upsert_job(conn, ingest.make_job(source="infopark", external_id="9", company="X", title="T",
                                                      location="Kochi", url="u", description_text="stored text"))
    assert ingest.known_jobs(conn, "infopark") == {"9": ("stored text", "Kochi")}


# ---- contact-domain rule must not flag legal-name vs mail-domain differences
def test_company_email_domain_matching():
    assert not any(f.kind == "contact_domain_mismatch"
                   for f in rules.scan_scam("Contact: careers@subhosting.net", "SubHosting Innovations (P) Ltd"))
    assert not any(f.kind == "contact_domain_mismatch"
                   for f in rules.scan_scam("Send CV to hr@experionglobal.com", "Experion Technologies"))
    assert not any(f.kind == "contact_domain_mismatch"           # real case seen on Technopark
                   for f in rules.scan_scam("Contact: hr@gnxsolutions.in", "GNX Digital Solutions (P) Ltd"))
    assert any(f.kind == "contact_domain_mismatch" for f in rules.scan_scam("Send CV to jobs@quickhire.xyz", "Experion Technologies"))
    assert any(f.kind == "free_email_recruiter" for f in rules.scan_scam("hr.experion@gmail.com", "Experion Technologies"))
