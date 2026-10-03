"""Ingestion (plan.md §7): fetch sources, normalise to one shape, upsert, dedupe, detect closures."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import random
import re
import socket
import time
import zlib
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin

import httpx

from . import config, db

log = logging.getLogger("hub.ingest")

# ---------------------------------------------------------------------------------- text helpers
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """HTML (possibly entity-escaped HTML, as Greenhouse returns) -> plain text. Output is only
    ever shown escaped or sent as untrusted text; links are not preserved."""
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    p = _TextExtractor()
    p.feed(raw)
    text = "".join(p.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


_SUFFIXES = re.compile(r"\b(inc|incorporated|ltd|limited|llc|llp|pvt|private|corp|corporation|gmbh|co|plc|technologies|technology)\b\.?", re.I)


def canonical_company(name: str, watchlist: list[dict] | None = None) -> str:
    n = (name or "").strip()
    for c in watchlist or []:
        if n.lower() in {c.get("name", "").lower(), c.get("canonical", "").lower()}:
            return c.get("canonical") or c["name"]
    n = _SUFFIXES.sub("", n.lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


CITY_ALIASES = {"bengaluru": "bangalore", "bangalore urban": "bangalore", "gurugram": "gurgaon",
                "bombay": "mumbai", "madras": "chennai", "calcutta": "kolkata", "new delhi": "delhi",
                "ncr": "delhi", "hyderabad, telangana": "hyderabad"}


def normalize_location(loc: str) -> str:
    s = re.sub(r"\s+", " ", (loc or "").lower()).strip()
    for a, b in CITY_ALIASES.items():
        s = re.sub(rf"\b{re.escape(a)}\b", b, s)
    s = re.sub(r"[–—-]+", " ", s)
    s = re.sub(r"[^a-z0-9, ]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split())


def desc_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text or "").strip().encode()).hexdigest()[:32]


def make_job(*, source, external_id, company, title, location, url, description_html="",
             description_text=None, posted_at=None, watchlist=None) -> dict:
    desc = description_text if description_text is not None else html_to_text(description_html)
    return {
        "source": source, "external_id": str(external_id), "company": company,
        "company_canonical": canonical_company(company, watchlist), "title": (title or "").strip(),
        "location_raw": (location or "").strip(), "location_norm": normalize_location(location or ""),
        "url": url, "description": desc, "description_hash": desc_hash(desc), "posted_at": posted_at,
    }


# ---------------------------------------------------------------------------------- fetching
_HTTP_CACHE = {"conn": None}


def set_http_cache(conn) -> None:
    """Enable the HTTP cache for subsequent fetches (None disables it). Set by run_all()."""
    _HTTP_CACHE["conn"] = conn


class CachedResponse:
    """Minimal stand-in for httpx.Response, built from the http_cache table."""
    def __init__(self, body: bytes, from_cache: str):
        self.content, self.from_cache = body, from_cache
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


def _cache_key(url: str, params: dict | None) -> str:
    if not params:
        return url
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


def _get(url: str, **kw):
    """GET with timeout, exponential backoff and (when enabled) an HTTP cache:
    fresh entries are reused without a request; stale ones are revalidated (304 -> reuse)."""
    conn = _HTTP_CACHE["conn"]
    key = _cache_key(url, kw.get("params"))
    cached = conn.execute("SELECT * FROM http_cache WHERE url=?", (key,)).fetchone() if conn is not None else None
    headers = {"User-Agent": "career-hub/0.1"}
    if cached is not None:
        if time.time() - cached["fetched_at"] < config.HTTP_CACHE_FRESH_SECONDS:
            return CachedResponse(zlib.decompress(cached["body"]), "fresh")
        if cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        if cached["last_modified"]:
            headers["If-Modified-Since"] = cached["last_modified"]
    last: Exception | None = None
    for attempt in range(config.SOURCE_RETRIES):
        try:
            r = httpx.get(url, timeout=config.FETCH_TIMEOUT, follow_redirects=True, headers=headers, **kw)
            if r.status_code == 304 and cached is not None:
                conn.execute("UPDATE http_cache SET fetched_at=? WHERE url=?", (time.time(), key))
                conn.commit()
                return CachedResponse(zlib.decompress(cached["body"]), "revalidated")
            r.raise_for_status()
            if conn is not None and len(r.content) <= config.FETCH_MAX_BYTES:
                conn.execute("INSERT OR REPLACE INTO http_cache (url, etag, last_modified, body, fetched_at) "
                             "VALUES (?,?,?,?,?)", (key, r.headers.get("etag"), r.headers.get("last-modified"),
                                                    zlib.compress(r.content), time.time()))
                conn.commit()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2 ** attempt, 8) + random.random())
    raise RuntimeError(f"{url}: {type(last).__name__}: {last}")


def _get_json(url: str, **kw):
    return _get(url, **kw).json()


def _polite() -> None:
    time.sleep(random.uniform(0.3, 0.9))


def known_jobs(conn, source: str) -> dict[str, tuple[str, str]]:
    """external_id -> (description, location) for jobs already stored, so detail pages are fetched once."""
    return {r["external_id"]: (r["description"] or "", r["location_raw"] or "")
            for r in conn.execute("SELECT external_id, description, location_raw FROM jobs WHERE source=?", (source,))}


# ---------------------------------------------------------------------------------- Kerala IT parks
INFOPARK = "https://infopark.in"
TECHNOPARK = "https://technopark.in"
_IP_ROW = re.compile(
    r'<tr>\s*<td class="head">([^<]*)</td>\s*<td class="head">(.*?)</td>\s*<td class="date">(.*?)</td>\s*'
    r'<td>(.*?)</td>\s*<td class="btn-sec">\s*<a href="([^"]*/company-jobs/details/(\d+)/(\d+))"', re.S)


def _ddmmyyyy(s: str) -> str | None:
    m = re.fullmatch(r"\s*(\d{2})-(\d{2})-(\d{4})\s*", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def parse_infopark_list(page_html: str) -> list[dict]:
    return [{"posted": _ddmmyyyy(m.group(1)), "title": html.unescape(m.group(2)).strip(),
             "company": html.unescape(m.group(3)).strip(), "last_date": html.unescape(m.group(4)).strip(),
             "url": m.group(5), "job_id": m.group(7)} for m in _IP_ROW.finditer(page_html)]


def parse_infopark_detail(page_html: str) -> dict:
    con = re.search(r'<div class="con">\s*<h4>(.*?)</h4>(.*?)</div>', page_html, re.S)
    company = html.unescape(con.group(1)).strip() if con else ""
    contacts = [html.unescape(x).strip() for x in re.findall(r"<span>(.*?)</span>", con.group(2))] if con else []
    s = page_html.find('class="deatil-box"')
    e = page_html.find("</section>", s) if s >= 0 else -1
    return {"company": company, "contacts": [c for c in contacts if c],
            "description_html": page_html[s:e] if s >= 0 and e > s else ""}


def fetch_infopark(wl, known: dict | None = None, max_pages: int = 40) -> list[dict]:
    """Infopark Kochi/Cherthala/Thrissur: paginated HTML table + one detail page per new job."""
    known, out, seen = known or {}, [], set()
    for page in range(1, max_pages + 1):
        url = f"{INFOPARK}/companies-job" + (f"?page={page}" if page > 1 else "")
        rows = [r for r in parse_infopark_list(_get(url).text) if r["job_id"] not in seen]
        if not rows:
            break
        for r in rows:
            seen.add(r["job_id"])
            if r["job_id"] in known:
                desc, loc = known[r["job_id"]]
            else:
                try:
                    _polite()
                    d = parse_infopark_detail(_get(r["url"]).text)
                except Exception as e:  # noqa: BLE001 - a failed detail is retried next run
                    log.warning("infopark detail %s failed: %s", r["url"], e)
                    continue
                body = html_to_text(d["description_html"])
                lm = re.search(r"Work Location\s*[-–:]\s*([^\n]+)", body)
                loc = lm.group(1).strip() if lm else "Infopark, Kerala"
                desc = (f"{body}\n\nCompany: {d['company'] or r['company']}\nContact: {' | '.join(d['contacts'])}\n"
                        f"Last date to apply: {r['last_date']}")
            out.append(make_job(source="infopark", external_id=r["job_id"], company=r["company"], title=r["title"],
                                location=loc, url=r["url"], description_text=desc, posted_at=r["posted"],
                                watchlist=wl))
    return out


def parse_inertia_page(page_html: str) -> dict:
    m = re.search(r'data-page="([^"]*)"', page_html)
    return json.loads(html.unescape(m.group(1))) if m else {}


def technopark_description(jl: dict) -> str:
    parts = [html_to_text(jl.get("job_description", ""))]
    if jl.get("preferred_skills"):
        parts.append("Qualifications / preferred skills:\n" + html_to_text(jl["preferred_skills"]))
    if jl.get("is_walk_in") and str(jl.get("is_walk_in")) not in ("0", "False"):
        parts.append(f"Walk-in drive on {jl.get('walk_in_start_date')} {jl.get('walk_in_start_time') or ''}"
                     f"-{jl.get('walk_in_end_time') or ''}.\n" + html_to_text(jl.get("walk_in_address") or ""))
    parts.append(f"Contact: {jl.get('contact_email') or 'not given'}\nLast date to apply: {jl.get('closing_date')}")
    return "\n\n".join(p for p in parts if p)


def fetch_technopark(wl, known: dict | None = None, max_pages: int = 60) -> list[dict]:
    """Technopark Trivandrum: public JSON list (/api/paginated-jobs) + one detail page per new job."""
    known, out = known or {}, []
    for page in range(1, max_pages + 1):
        data = _get_json(f"{TECHNOPARK}/api/paginated-jobs", params={"page": page, "search": "", "type": ""})
        for j in data.get("data", []):
            ext, url = str(j["id"]), f"{TECHNOPARK}/job-details/{j['id']}"
            if ext in known:
                desc, loc = known[ext]
            else:
                try:
                    _polite()
                    jl = parse_inertia_page(_get(url).text)["props"]["jobListing"]
                except Exception as e:  # noqa: BLE001
                    log.warning("technopark detail %s failed: %s", url, e)
                    continue
                desc, loc = technopark_description(jl), "Technopark, Thiruvananthapuram, Kerala"
            out.append(make_job(source="technopark", external_id=ext, company=(j.get("company") or {}).get("company"),
                                title=j.get("job_title"), location=loc, url=url, description_text=desc,
                                posted_at=j.get("posted_date"), watchlist=wl))
        if data.get("current_page", page) >= data.get("last_page", page):
            break
    return out


PRIORITY_FETCHERS = {"infopark": fetch_infopark, "technopark": fetch_technopark}


def fetch_greenhouse(c: dict, wl) -> list[dict]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs?content=true")
    return [make_job(source=f"greenhouse:{c['token']}", external_id=j["id"], company=c["name"], title=j.get("title"),
                     location=(j.get("location") or {}).get("name"), url=j.get("absolute_url"),
                     description_html=j.get("content", ""), posted_at=j.get("updated_at"), watchlist=wl)
            for j in data.get("jobs", [])]


def fetch_lever(c: dict, wl) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{c['token']}?mode=json")
    out = []
    for j in data:
        desc = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
        for lst in j.get("lists", []):
            desc += f"\n\n{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}"
        out.append(make_job(source=f"lever:{c['token']}", external_id=j["id"], company=c["name"], title=j.get("text"),
                            location=(j.get("categories") or {}).get("location"), url=j.get("hostedUrl"),
                            description_text=desc.strip(), watchlist=wl))
    return out


def fetch_ashby(c: dict, wl) -> list[dict]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}?includeCompensation=true")
    return [make_job(source=f"ashby:{c['token']}", external_id=j["id"], company=c["name"], title=j.get("title"),
                     location=j.get("location"), url=j.get("jobUrl"),
                     description_html=j.get("descriptionHtml", ""), posted_at=j.get("publishedAt"), watchlist=wl)
            for j in data.get("jobs", [])]


def fetch_remotive(wl) -> list[dict]:
    data = _get_json("https://remotive.com/api/remote-jobs?category=software-dev")
    return [make_job(source="remotive", external_id=j["id"], company=j.get("company_name"), title=j.get("title"),
                     location=j.get("candidate_required_location"), url=j.get("url"),
                     description_html=j.get("description", ""), posted_at=j.get("publication_date"), watchlist=wl)
            for j in data.get("jobs", [])]


def fetch_himalayas(wl) -> list[dict]:
    data = _get_json("https://himalayas.app/jobs/api?limit=100")
    out = []
    for j in data.get("jobs", []):
        ext = j.get("guid") or j.get("applicationLink") or j.get("title")
        loc = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
        out.append(make_job(source="himalayas", external_id=ext, company=j.get("companyName"), title=j.get("title"),
                            location=loc, url=j.get("applicationLink") or j.get("guid"),
                            description_html=j.get("description", ""), posted_at=j.get("pubDate"), watchlist=wl))
    return out


def fetch_jobspy(prefs: dict, wl) -> list[dict]:
    """Secondary, slow source. Randomised delays between searches; may be blocked (see §8)."""
    from jobspy import scrape_jobs  # imported lazily: heavy, optional
    cfg = prefs.get("jobspy", {})
    out = []
    for s in cfg.get("searches", []):
        df = scrape_jobs(site_name=s.get("sites", ["naukri"]), search_term=s["term"], location=s.get("location"),
                         results_wanted=cfg.get("results_wanted", 20), country_indeed=s.get("country", "India"),
                         description_format="markdown")
        for _, r in df.iterrows():
            out.append(make_job(source=f"jobspy:{r.get('site')}", external_id=r.get("id") or r.get("job_url"),
                                company=str(r.get("company") or ""), title=r.get("title"),
                                location=str(r.get("location") or ""), url=r.get("job_url"),
                                description_text=str(r.get("description") or ""),
                                posted_at=str(r.get("date_posted") or "") or None, watchlist=wl))
        time.sleep(random.uniform(5, 15))
    return out


# ---------------------------------------------------------------------------------- safe URL fetch
class UnsafeURL(ValueError):
    pass


def check_url_safe(url: str) -> str:
    """http/https only; reject localhost, private, link-local and reserved addresses (resolved)."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https"):
        raise UnsafeURL("only http/https URLs are accepted")
    host = u.hostname
    if not host:
        raise UnsafeURL("URL has no host")
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localhost")):
        raise UnsafeURL("local hosts are not allowed")
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve host: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            raise UnsafeURL(f"address {ip} is not a public address")
    return url.strip()


def safe_fetch(url: str, max_redirects: int = 3) -> tuple[str, str]:
    """Returns (final_url, body_text). Manual redirects so each hop is re-validated; size capped."""
    for _ in range(max_redirects + 1):
        check_url_safe(url)
        with httpx.stream("GET", url, timeout=config.FETCH_TIMEOUT, follow_redirects=False,
                          headers={"User-Agent": "career-hub/0.1"}) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            r.raise_for_status()
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > config.FETCH_MAX_BYTES:
                    raise UnsafeURL("response too large")
            return url, buf.decode(r.encoding or "utf-8", errors="replace")
    raise UnsafeURL("too many redirects")


def parse_posting_page(url: str, body: str) -> dict:
    """Best-effort: JSON-LD JobPosting first, else <title> + visible text."""
    title = company = location = None
    desc_html = ""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                title = item.get("title")
                org = item.get("hiringOrganization")
                company = org.get("name") if isinstance(org, dict) else org
                loc = item.get("jobLocation")
                loc = loc[0] if isinstance(loc, list) and loc else loc
                addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
                location = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressCountry")
                                                   if isinstance(addr.get("addressCountry"), str) else None]))
                desc_html = item.get("description", "")
                break
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        title = html.unescape(m.group(1)).strip() if m else url
    if not desc_html:
        desc_html = body
    host = urlparse(url).hostname or ""
    return {"title": title, "company": company or host, "location": location or "", "description_html": desc_html}


def add_url_job(conn, url: str, watchlist=None) -> int:
    final_url, body = safe_fetch(url)
    p = parse_posting_page(final_url, body)
    job = make_job(source="manual", external_id=hashlib.sha1(final_url.encode()).hexdigest()[:16],
                   company=p["company"], title=p["title"], location=p["location"], url=final_url,
                   description_html=p["description_html"], watchlist=watchlist)
    job_id, _ = upsert_job(conn, job)
    return job_id


# ---------------------------------------------------------------------------------- store
REOPENABLE = ("filtered_out", "ineligible", "needs_review", "scored")


def upsert_job(conn, job: dict) -> tuple[int, str]:
    """Returns (job_id, outcome) with outcome in new | unchanged | changed."""
    row = conn.execute("SELECT * FROM jobs WHERE source=? AND external_id=?",
                       (job["source"], job["external_id"])).fetchone()
    if row is None:
        return db.insert_job(conn, job), "new"
    ts = db.now()
    if row["description_hash"] == job["description_hash"]:
        conn.execute("UPDATE jobs SET last_seen=?, missed_runs=0 WHERE id=?", (ts, row["id"]))
        conn.commit()
        return row["id"], "unchanged"
    conn.execute("""UPDATE jobs SET description=?, description_hash=?, title=?, location_raw=?, location_norm=?,
                    url=?, last_seen=?, missed_runs=0, last_error=NULL WHERE id=?""",
                 (job["description"], job["description_hash"], job["title"], job["location_raw"],
                  job["location_norm"], job["url"], ts, row["id"]))
    conn.commit()
    if row["status"] in REOPENABLE:
        db.transition(conn, row["id"], "new", "description changed; re-running eligibility")
    else:
        db.log_event(conn, row["id"], "description changed")
    return row["id"], "changed"


def mark_missing(conn, source: str, seen_ids: set[str]) -> int:
    """For a complete ATS feed that fetched successfully: bump missed_runs for absent jobs;
    two consecutive misses close the job (pre-applied states only). Returns closed count."""
    closed = 0
    rows = conn.execute("SELECT id, external_id, status, missed_runs FROM jobs WHERE source=?", (source,)).fetchall()
    for r in rows:
        if r["external_id"] in seen_ids or r["status"] not in db.PRE_APPLIED:
            continue
        misses = r["missed_runs"] + 1
        conn.execute("UPDATE jobs SET missed_runs=? WHERE id=?", (misses, r["id"]))
        conn.commit()
        if misses >= 2:
            db.transition(conn, r["id"], "closed", "missing from source feed for 2 consecutive runs")
            closed += 1
    return closed


def mark_duplicates(conn, threshold: float = 0.85) -> int:
    """Cross-source duplicates: same canonical company + title + location, similar description."""
    rows = conn.execute("""SELECT id, company_canonical, title, location_norm, description FROM jobs
                           WHERE dup_of IS NULL ORDER BY id""").fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["company_canonical"], normalize_title(r["title"]), r["location_norm"]), []).append(r)
    n = 0
    for members in groups.values():
        primary = members[:1]
        for r in members[1:]:
            for p in primary:
                sim = SequenceMatcher(None, (p["description"] or "")[:2000], (r["description"] or "")[:2000]).ratio()
                if sim >= threshold:
                    conn.execute("UPDATE jobs SET dup_of=? WHERE id=?", (p["id"], r["id"]))
                    n += 1
                    break
            else:
                primary.append(r)
    conn.commit()
    return n


# ---------------------------------------------------------------------------------- runner
def load_watchlist() -> list[dict]:
    return config.load_yaml("companies.yaml").get("companies", [])


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def _store(conn, jobs: list[dict]) -> int:
    new = 0
    for j in jobs:
        _, outcome = upsert_job(conn, j)
        new += outcome == "new"
    return new


def run_source(conn, name: str, fn, *, complete_feed_source: str | None = None) -> dict:
    """Fetch one source in isolation. A failure is recorded in `runs`, never raised."""
    started = db.now()
    fetched = new = 0
    err = None
    try:
        jobs = fn()
        fetched = len(jobs)
        new = _store(conn, jobs)
        if complete_feed_source:
            mark_missing(conn, complete_feed_source, {j["external_id"] for j in jobs})
    except Exception as e:  # noqa: BLE001 - one failing source must never abort the run
        err = f"{type(e).__name__}: {e}"[:500]
        log.warning("source %s failed: %s", name, err)
    conn.execute("INSERT INTO runs (started_at, finished_at, source, jobs_fetched, jobs_new, errors) VALUES (?,?,?,?,?,?)",
                 (started, db.now(), name, fetched, new, err))
    conn.commit()
    return {"source": name, "fetched": fetched, "new": new, "error": err}


def run_all(conn, only: str | None = None) -> list[dict]:
    prefs = config.load_prefs()
    wl = load_watchlist()
    results = []
    set_http_cache(conn)
    try:
        return _run_all(conn, prefs, wl, results, only)
    finally:
        set_http_cache(None)


def _run_all(conn, prefs, wl, results, only):
    for name in prefs.get("priority_sources", []):          # Kerala IT parks first (plan: user priority)
        fn = PRIORITY_FETCHERS.get(name)
        if fn and (not only or only in name):
            results.append(run_source(conn, name, lambda fn=fn, name=name: fn(wl, known_jobs(conn, name)),
                                      complete_feed_source=name))
    for c in wl:
        fetcher = FETCHERS.get(c.get("ats"))
        if not fetcher:
            continue
        name = f"{c['ats']}:{c['token']}"
        if only and only not in name:
            continue
        results.append(run_source(conn, name, lambda c=c, f=fetcher: f(c, wl), complete_feed_source=name))
    for name, fn in (("remotive", lambda: fetch_remotive(wl)), ("himalayas", lambda: fetch_himalayas(wl))):
        if not only or only in name:
            results.append(run_source(conn, name, fn))
    if prefs.get("jobspy", {}).get("searches") and (not only or only == "jobspy"):
        results.append(run_source(conn, "jobspy", lambda: fetch_jobspy(prefs, wl)))
    mark_duplicates(conn)
    return results
