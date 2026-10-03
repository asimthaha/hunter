"""`hub` command line. Thin: every operation calls src/actions.py (or pipeline) — no business logic here."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser

from . import actions, config, db, digest, pipeline, report
from .llm import LLM


def _conn():
    return db.connect()


def _llm(conn) -> LLM:
    return LLM(conn=conn)


def _print_jobs(rows) -> None:
    for r in rows:
        print(f"[{r['id']:>4}] {r['status']:<12} {r.get('decision') or '':<6} {r['company'] or '':<24.24} {r['title'] or '':<48.48} {r['bucket'] or ''}")


def cmd_init(a, conn):
    for d in (config.DATA_DIR, config.REPORTS_DIR, config.OUTPUT_DIR, config.LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for ex in sorted(config.DATA_DIR.glob("*.example.yaml")):
        target = ex.with_name(ex.name.replace(".example", ""))
        if not target.exists():
            target.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"created {target.relative_to(config.ROOT)} from example — edit it")
    print("database ready:", config.DB_PATH)


def cmd_run(a, conn):
    res = pipeline.run(conn, _llm(conn), fetch=not a.no_fetch, notify=not a.no_notify, only=a.only)
    for s in res["sources"]:
        print(f"{s['source']}: fetched {s['fetched']} new {s['new']}" + (f"  ERROR {s['error']}" if s["error"] else ""))
    print("processed:", res["processed"])
    print("digest:", res["digest"])


def cmd_score(a, conn):
    print("processed:", pipeline.process_new(conn, _llm(conn)))


def cmd_review(a, conn):
    rows = actions.inbox(conn, a.bucket, a.decision)
    if not rows:
        print("inbox empty")
        return
    keys = {"s": "shortlist", "k": "skip", "i": "ineligible"}
    for r in rows:
        print(f"\n[{r['id']}] {r['title']} — {r['company']}  ({r['bucket']}, {r['decision'] or r['status']}, pay {r['pay_status']})")
        print("   scores:", r["scores"], "|", r["status_reason"])
        while True:
            c = input("  (s)hortlist (k)skip (i)neligible (v)iew (n)ext (q)uit > ").strip().lower()
            if c == "q":
                return
            if c == "n":
                break
            if c == "v":
                print(report.build(conn, r["id"]))
            elif c in keys:
                actions.review(conn, r["id"], keys[c])
                break


def cmd_show(a, conn):
    print(report.build(conn, a.id))


def cmd_list(a, conn):
    q, args = "SELECT * FROM jobs", []
    if a.status:
        q, args = q + " WHERE status=?", [a.status]
    _print_jobs([dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [a.limit])])


def cmd_shortlist(a, conn):
    if a.id is None:
        _print_jobs([dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status='shortlisted' ORDER BY id")])
        return
    actions.review(conn, a.id, "shortlist")
    print("shortlisted", a.id)


def cmd_skip(a, conn):
    actions.review(conn, a.id, "skip")


def cmd_tailor(a, conn):
    res = actions.tailor(conn, _llm(conn), a.id, a.force)
    print("OK:" if res["ok"] else "FAILED:", res.get("path") or "; ".join(res["errors"]))


def cmd_draft(a, conn):
    res = actions.draft(conn, _llm(conn), a.id, a.question)
    for d in res.get("drafts", []):
        print(f"\n--- {d['kind']} {d.get('question', '')}\n{d['text'] or 'REJECTED: ' + '; '.join(d['errors'])}")
    for q in res.get("answer_yourself_questions", []):
        print(f"\nANSWER YOURSELF: {q}")


def cmd_applied(a, conn):
    print("follow-up on", actions.mark_applied(conn, a.id, a.follow_up_days))


def cmd_status(a, conn):
    actions.set_status(conn, a.id, a.status, a.reason or "")


def cmd_contact_sent(a, conn):
    actions.contact_sent(conn, a.id)


def cmd_add_url(a, conn):
    job_id, status = actions.add_url(conn, _llm(conn), a.url)
    print(f"job {job_id}: {status}")


def cmd_audit(a, conn):
    for r in actions.audit_sample(conn, a.n):
        print(f"[{r['id']}] {r['status']:<12} {r['title']} — {r['company']}\n      reason: {r['status_reason']}")
    print("\nWould you have applied to any? `hub golden <id>` saves it to tests/golden/.")


def cmd_golden(a, conn):
    print("saved", actions.save_golden(conn, a.id, a.label))


def cmd_export(a, conn):
    sys.stdout.write(actions.export_csv(conn))


def cmd_digest(a, conn):
    print(digest.write(conn, notify=not a.no_notify))


def cmd_ghosted(a, conn):
    print("marked ghosted:", actions.sweep_ghosted(conn, a.grace_days))


def cmd_calibrate(a, conn):
    from . import calibrate
    m = calibrate.run(_llm(conn))
    print(json.dumps(m, indent=2))
    print("GO" if m["go"] else "NO-GO (need >=50 cases, recall >=85%, precision >=70%)")


def cmd_ui(a, conn):
    import threading
    import uvicorn
    from .web.app import create_app
    app = create_app(port=a.port)
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hub", description="Career Command Hub")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, help="create folders, DB, and data/*.yaml from the examples")
    sp = add("run", cmd_run, help="ingest -> filter -> score -> digest")
    sp.add_argument("--no-fetch", action="store_true")
    sp.add_argument("--no-notify", action="store_true")
    sp.add_argument("--only", help="fetch only sources whose name contains this (e.g. technopark)")
    add("score", cmd_score, help="score jobs already ingested")
    sp = add("review", cmd_review, help="interactive inbox")
    sp.add_argument("--bucket")
    sp.add_argument("--decision", choices=["apply", "maybe"])
    sp = add("show", cmd_show, help="print a job report")
    sp.add_argument("id", type=int)
    sp = add("list", cmd_list)
    sp.add_argument("--status", choices=db.ALL_STATUSES)
    sp.add_argument("--limit", type=int, default=50)
    sp = add("shortlist", cmd_shortlist, help="shortlist a job (no id: list shortlisted)")
    sp.add_argument("id", type=int, nargs="?")
    sp = add("skip", cmd_skip)
    sp.add_argument("id", type=int)
    sp = add("tailor", cmd_tailor, help="build resume PDF for a shortlisted job")
    sp.add_argument("id", type=int)
    sp.add_argument("--force", action="store_true")
    sp = add("draft", cmd_draft, help="grounded referral note / screening answers")
    sp.add_argument("id", type=int)
    sp.add_argument("--question", "-q", action="append", default=[])
    sp = add("applied", cmd_applied, help="record that YOU submitted an application")
    sp.add_argument("id", type=int)
    sp.add_argument("--follow-up-days", type=int)
    sp = add("status", cmd_status, help="interviewing | offer | rejected | ghosted")
    sp.add_argument("id", type=int)
    sp.add_argument("status", choices=["interviewing", "offer", "rejected", "ghosted"])
    sp.add_argument("--reason")
    sp = add("contact-sent", cmd_contact_sent, help="record that YOU sent a LinkedIn/referral message")
    sp.add_argument("id", type=int)
    sp = add("add-url", cmd_add_url)
    sp.add_argument("url")
    sp = add("audit", cmd_audit, help="weekly sample of rejected jobs")
    sp.add_argument("-n", type=int)
    sp = add("golden", cmd_golden, help="save a job to tests/golden/ as a labelled case")
    sp.add_argument("id", type=int)
    sp.add_argument("--label", default="apply", choices=["apply", "maybe", "skip"])
    add("export", cmd_export, help="tracker as CSV on stdout")
    sp = add("digest", cmd_digest)
    sp.add_argument("--no-notify", action="store_true")
    sp = add("ghosted", cmd_ghosted, help="mark applied jobs past follow-up as ghosted")
    sp.add_argument("--grace-days", type=int, default=7)
    add("calibrate", cmd_calibrate, help="precision/recall on tests/golden/ (calls the API)")
    sp = add("ui", cmd_ui, help="local web UI on 127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=config.LOGS_DIR / "hub.log",
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    conn = _conn()
    try:
        args.fn(args, conn)
    except (actions.ActionError, db.InvalidTransition) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
