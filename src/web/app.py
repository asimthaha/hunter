"""Minimal local UI (plan.md §11a): a thin layer over actions.py.

Security: bind 127.0.0.1 (see cli `hub ui`), Host-header allow-list, per-start CSRF token on every
POST (+ SameSite=Strict cookie), no state change on GET, CSP default-src 'self', autoescaped
templates, files served by job-ID lookup only.
"""
from __future__ import annotations

import functools
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from .. import actions, config, db
from ..llm import LLM

STATIC_DIR = Path(__file__).parent / "static"
CSP = "default-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
COOKIE = "hub_csrf"


def create_app(db_path: str | Path | None = None, port: int = 8765, csrf_token: str | None = None,
               llm_factory=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.csrf = csrf_token or secrets.token_urlsafe(32)
    app.state.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app.state.db_path = db_path
    app.state.llm_factory = llm_factory or (lambda conn: LLM(conn=conn))
    env = Environment(loader=FileSystemLoader(str(config.TEMPLATES_DIR / "web")), autoescape=True)
    templates = Jinja2Templates(env=env)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---------------------------------------------------------------- middleware
    @app.middleware("http")
    async def secure(request: Request, call_next):
        if request.headers.get("host", "") not in app.state.allowed_hosts:
            return PlainTextResponse("invalid host", status_code=400)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.cookies.get(COOKIE) != app.state.csrf:
            response.set_cookie(COOKIE, app.state.csrf, httponly=True, samesite="strict", path="/")
        return response

    # ---------------------------------------------------------------- dependencies
    def get_conn():
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    async def check_csrf(request: Request) -> None:
        token = request.headers.get("x-csrf-token")
        if token is None:
            token = (await request.form()).get("csrf_token")
        cookie = request.cookies.get(COOKIE)
        if not token or not cookie or not secrets.compare_digest(token, app.state.csrf) \
                or not secrets.compare_digest(cookie, app.state.csrf):
            raise HTTPException(403, "bad CSRF token")

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.update(csrf=app.state.csrf, request=request,
                   htmx_headers='{"X-CSRF-Token": "%s"}' % app.state.csrf)
        return templates.TemplateResponse(request, name, ctx)

    def guarded(fn):
        """Turn action/state-machine errors into a 400 with a plain message."""
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except (actions.ActionError, db.InvalidTransition) as e:
                raise HTTPException(400, str(e)) from e
        return wrapper

    def refresh() -> Response:
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    # ---------------------------------------------------------------- pages (GET: read-only)
    @app.get("/", response_class=HTMLResponse)
    def inbox(request: Request, bucket: str | None = None, decision: str | None = None, conn=Depends(get_conn)):
        rows = actions.inbox(conn, bucket or None, decision or None)
        return render(request, "inbox.html", rows=rows, bucket=bucket or "", decision=decision or "")

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int, conn=Depends(get_conn)):
        try:
            detail = actions.job_detail(conn, job_id)
        except actions.ActionError:
            raise HTTPException(404, "no such job")
        return render(request, "job.html", d=detail, tracker_statuses=["interviewing", "offer", "rejected", "ghosted"])

    @app.get("/job/{job_id}/resume.pdf")
    def resume_pdf(job_id: int, conn=Depends(get_conn)):
        path = db.get_artifact(conn, job_id, "resume_pdf")
        if not path or not Path(path).is_file():
            raise HTTPException(404, "no resume for this job")
        # Defence in depth: the stored path must live inside output/.
        if config.OUTPUT_DIR.resolve() not in Path(path).resolve().parents:
            raise HTTPException(404, "no resume for this job")
        return FileResponse(path, media_type="application/pdf", filename=f"resume-{job_id}.pdf")

    @app.get("/job/{job_id}/report.md")
    def report_md(job_id: int, conn=Depends(get_conn)):
        path = db.get_artifact(conn, job_id, "report")
        if not path or not Path(path).is_file() or config.REPORTS_DIR.resolve() not in Path(path).resolve().parents:
            raise HTTPException(404, "no report for this job")
        return PlainTextResponse(Path(path).read_text(encoding="utf-8"))

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker_page(request: Request, conn=Depends(get_conn)):
        return render(request, "tracker.html", rows=actions.tracker(conn),
                      statuses=["applied", "interviewing", "offer", "rejected", "ghosted"])

    @app.get("/tracker.csv")
    def tracker_csv(conn=Depends(get_conn)):
        return Response(actions.export_csv(conn), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tracker.csv"})

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals_page(request: Request, conn=Depends(get_conn)):
        return render(request, "proposals.html", rows=actions.proposals(conn))

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request, conn=Depends(get_conn)):
        return render(request, "health.html", h=actions.health(conn))

    # ---------------------------------------------------------------- actions (POST + CSRF)
    @app.post("/job/{job_id}/review", dependencies=[Depends(check_csrf)])
    @guarded
    def post_review(job_id: int, action: str = Form(...), redirect: str = Form(""), conn=Depends(get_conn)):
        actions.review(conn, job_id, action)
        return Response(status_code=200, headers={"HX-Refresh": "true"}) if redirect else HTMLResponse("")

    @app.post("/job/{job_id}/applied", dependencies=[Depends(check_csrf)])
    @guarded
    def post_applied(job_id: int, follow_up_days: int | None = Form(None), conn=Depends(get_conn)):
        actions.mark_applied(conn, job_id, follow_up_days)
        return refresh()

    @app.post("/job/{job_id}/contact-sent", dependencies=[Depends(check_csrf)])
    @guarded
    def post_contact(job_id: int, conn=Depends(get_conn)):
        actions.contact_sent(conn, job_id)
        return refresh()

    @app.post("/job/{job_id}/status", dependencies=[Depends(check_csrf)])
    @guarded
    def post_status(job_id: int, status: str = Form(...), conn=Depends(get_conn)):
        actions.set_status(conn, job_id, status)
        return refresh()

    @app.post("/job/{job_id}/follow-up", dependencies=[Depends(check_csrf)])
    @guarded
    def post_follow_up(job_id: int, date: str = Form(""), conn=Depends(get_conn)):
        actions.set_follow_up(conn, job_id, date or None)
        return refresh()

    @app.post("/job/{job_id}/tailor", dependencies=[Depends(check_csrf)])
    @guarded
    def post_tailor(job_id: int, conn=Depends(get_conn)):
        res = actions.tailor(conn, app.state.llm_factory(conn), job_id)
        if not res["ok"]:
            raise HTTPException(400, "; ".join(res["errors"])[:500])
        return refresh()

    @app.post("/job/{job_id}/draft", dependencies=[Depends(check_csrf)])
    @guarded
    def post_draft(job_id: int, question: str = Form(""), conn=Depends(get_conn)):
        res = actions.draft(conn, app.state.llm_factory(conn), job_id, [question] if question.strip() else [])
        if not res.get("ok"):
            raise HTTPException(400, res.get("error", "draft failed")[:500])
        return refresh()

    @app.post("/proposals/{approval_id}/decide", dependencies=[Depends(check_csrf)])
    @guarded
    def post_decide(approval_id: int, decision: str = Form(...), conn=Depends(get_conn)):
        actions.decide_proposal(conn, approval_id, decision == "approve")
        return HTMLResponse("")

    @app.post("/health/golden/{job_id}", dependencies=[Depends(check_csrf)])
    @guarded
    def post_golden(job_id: int, conn=Depends(get_conn)):
        actions.save_golden(conn, job_id, "apply")
        return HTMLResponse("saved to tests/golden/")

    @app.post("/add-url", dependencies=[Depends(check_csrf)])
    @guarded
    def post_add_url(url: str = Form(...), conn=Depends(get_conn)):
        job_id, _ = actions.add_url(conn, app.state.llm_factory(conn), url)
        return Response(status_code=200, headers={"HX-Redirect": f"/job/{job_id}"})

    return app
