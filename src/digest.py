"""Daily digest (plan.md §7, §8, §10): markdown file + Windows toast."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import config, db


def spend(conn) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month = today[:7]
    q = "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE at LIKE ?"
    return {"today": conn.execute(q, (today + "%",)).fetchone()[0], "month": conn.execute(q, (month + "%",)).fetchone()[0]}


def source_health(conn) -> list[dict]:
    """Per-source last run + warnings (errors, or 0 jobs for 2 consecutive runs)."""
    out = []
    for r in conn.execute("SELECT DISTINCT source FROM runs ORDER BY source").fetchall():
        runs = conn.execute("SELECT * FROM runs WHERE source=? ORDER BY run_id DESC LIMIT 2", (r["source"],)).fetchall()
        last = runs[0]
        warnings = []
        if last["errors"]:
            warnings.append(f"last run failed: {last['errors']}")
        if len(runs) == 2 and all(x["jobs_fetched"] == 0 for x in runs):
            warnings.append("returned 0 jobs for 2 consecutive runs (token changed / endpoint moved / blocked?)")
        out.append({"source": r["source"], "at": last["finished_at"], "fetched": last["jobs_fetched"],
                    "new": last["jobs_new"], "warnings": warnings})
    return out


def cooldown_warnings(conn, days: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT DISTINCT j.company_canonical AS c, j.company AS name FROM jobs j JOIN job_events e ON e.job_id=j.id
           WHERE e.to_status='applied' AND e.at>=?""", (cutoff,)).fetchall()
    out = []
    for r in rows:
        n = conn.execute("""SELECT COUNT(*) FROM jobs WHERE company_canonical=? AND status IN ('scored','shortlisted','tailored')""",
                         (r["c"],)).fetchone()[0]
        if n:
            out.append(f"{r['name']}: applied within {days} days; {n} more open candidate(s) — consider waiting")
    return out


def build(conn, since_hours: int = 24) -> str:
    prefs = config.load_prefs()
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    date = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Digest {date}", ""]

    def job_line(j):
        return f"- [{j['id']}] {j['title']} — {j['company']} ({j['bucket'] or '?'}) — `hub show {j['id']}`"

    for dec in ("apply", "maybe"):
        rows = conn.execute(
            """SELECT j.* FROM jobs j JOIN scores s ON s.job_id=j.id AND s.stage='evaluate' AND s.description_hash=j.description_hash
               WHERE j.status='scored' AND s.decision=? AND s.created_at>=? AND j.dup_of IS NULL ORDER BY j.id""",
            (dec, since)).fetchall()
        lines += [f"## New `{dec}` ({len(rows)})", ""] + ([job_line(j) for j in rows] or ["_none_"]) + [""]
    nr = conn.execute("SELECT * FROM jobs WHERE status='needs_review' ORDER BY id").fetchall()
    lines += [f"## Needs review ({len(nr)})", ""] + ([job_line(j) + f" — {j['status_reason']}" for j in nr] or ["_none_"]) + [""]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    od = conn.execute("SELECT * FROM jobs WHERE status='applied' AND follow_up_on IS NOT NULL AND follow_up_on<=? ORDER BY follow_up_on",
                      (today,)).fetchall()
    if od:
        lines += [f"## Follow-ups due ({len(od)})", ""] + [job_line(j) + f" — due {j['follow_up_on']}" for j in od] + [""]
    cd = cooldown_warnings(conn, prefs["company_cooldown_days"])
    if cd:
        lines += ["## Company cooldown", ""] + [f"- {c}" for c in cd] + [""]
    lines += ["## Source health", ""]
    health = source_health(conn)
    lines += [f"- {h['source']}: fetched {h['fetched']}, new {h['new']}" + "".join(f"\n  - WARNING: {w}" for w in h["warnings"])
              for h in health] or ["_no runs yet_"]
    sp = spend(conn)
    lines += ["", "## Spend (estimate)", "", f"- Today: ${sp['today']:.2f}", f"- Month to date: ${sp['month']:.2f}",
              "- Compare with the spend limit in the Anthropic console.", ""]
    return "\n".join(lines)


def write(conn, notify: bool = True) -> str:
    text = build(conn)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / f"digest-{datetime.now().strftime('%Y-%m-%d')}.md"
    path.write_text(text, encoding="utf-8")
    if notify:
        toast("Career Hub digest ready", str(path))
    return str(path)


def toast(title: str, body: str) -> bool:
    """Best-effort Windows toast. Strings are XML-escaped and passed via environment variables."""
    if sys.platform != "win32":
        return False
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
        "$n=$t.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($t.CreateTextNode($env:HUB_TOAST_TITLE)) > $null;"
        "$n.Item(1).AppendChild($t.CreateTextNode($env:HUB_TOAST_BODY)) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Career Hub').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        import os
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=15,
                       capture_output=True, env={**os.environ, "HUB_TOAST_TITLE": title, "HUB_TOAST_BODY": body})
        return True
    except Exception:  # noqa: BLE001
        return False
