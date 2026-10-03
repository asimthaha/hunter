"""API usage and caching report, built from the llm_usage table (token counts only — no request bodies)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config


def summary(conn, days: int | None = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds") if days else ""
    rows = conn.execute(
        """SELECT stage, model, COUNT(*) AS calls, SUM(local_cache_hit) AS local_hits,
                  SUM(input_tokens) AS inp, SUM(output_tokens) AS out, SUM(cache_read_tokens) AS cr,
                  SUM(cache_write_tokens) AS cw, SUM(cost_usd) AS cost
           FROM llm_usage WHERE at >= ? GROUP BY stage, model ORDER BY cost DESC""", (since,)).fetchall()
    out_rows, tot = [], {"calls": 0, "api_calls": 0, "local_hits": 0, "cost": 0.0, "saved_prompt_cache": 0.0,
                         "saved_local": 0.0, "cr": 0, "prompt_tokens": 0}
    for r in rows:
        price_in = config.PRICES.get(r["model"], {"in": 2.0})["in"]
        api_calls = r["calls"] - r["local_hits"]
        prompt_tokens = r["inp"] + r["cr"] + r["cw"]
        # reads cost 0.1x instead of 1x; writes cost 1.25x instead of 1x (the premium paid to get reads)
        saved_pc = (r["cr"] * price_in * (1 - config.CACHE_READ_MULT)
                    - r["cw"] * price_in * (config.CACHE_WRITE_MULT - 1)) / 1_000_000
        saved_local = (r["cost"] / api_calls) * r["local_hits"] if api_calls else 0.0
        minimum = config.CACHE_MIN_TOKENS.get(r["model"])
        hint = ""
        if api_calls >= 2 and r["cr"] == 0 and r["cw"] == 0:
            hint = (f"no prompt caching: the prefix is probably under the {minimum}-token minimum for this model"
                    if minimum else "no prompt caching observed")
        elif api_calls >= 3 and r["cw"] > 0 and r["cr"] == 0:
            hint = "cache written but never read: something in the cached prefix changes between calls"
        out_rows.append({
            "stage": r["stage"], "model": r["model"], "calls": r["calls"], "api_calls": api_calls,
            "local_hits": r["local_hits"], "input_tokens": r["inp"], "output_tokens": r["out"],
            "cache_read_tokens": r["cr"], "cache_write_tokens": r["cw"], "cost": r["cost"],
            "cache_read_ratio": (r["cr"] / prompt_tokens) if prompt_tokens else 0.0,
            "saved_prompt_cache": saved_pc, "saved_local": saved_local, "hint": hint})
        tot["calls"] += r["calls"]
        tot["api_calls"] += api_calls
        tot["local_hits"] += r["local_hits"]
        tot["cost"] += r["cost"]
        tot["saved_prompt_cache"] += saved_pc
        tot["saved_local"] += saved_local
        tot["cr"] += r["cr"]
        tot["prompt_tokens"] += prompt_tokens
    tot["cache_read_ratio"] = tot["cr"] / tot["prompt_tokens"] if tot["prompt_tokens"] else 0.0
    cache = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS hits FROM llm_cache").fetchone()
    http = conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0]
    return {"days": days, "rows": out_rows, "totals": tot, "local_cache_entries": cache["n"],
            "http_cache_entries": http}


def format_report(s: dict) -> str:
    t = s["totals"]
    lines = [f"API usage, last {s['days']} days (cost is an ESTIMATE; compare with the console)", ""]
    if not s["rows"]:
        return "\n".join(lines + ["no API calls recorded yet"])
    lines.append(f"{'stage':<12}{'model':<28}{'calls':>6}{'local':>6}{'input':>10}{'output':>9}{'cache_rd':>10}"
                 f"{'cache_wr':>10}{'rd%':>5}{'cost$':>9}")
    for r in s["rows"]:
        lines.append(f"{r['stage']:<12}{r['model'][:27]:<28}{r['calls']:>6}{r['local_hits']:>6}{r['input_tokens']:>10}"
                     f"{r['output_tokens']:>9}{r['cache_read_tokens']:>10}{r['cache_write_tokens']:>10}"
                     f"{r['cache_read_ratio'] * 100:>4.0f}%{r['cost']:>9.4f}")
        if r["hint"]:
            lines.append(f"    ! {r['hint']}")
    lines += ["",
              f"Total estimated spend: ${t['cost']:.4f} over {t['api_calls']} API calls",
              f"Prompt caching: {t['cache_read_ratio'] * 100:.0f}% of prompt tokens served from cache "
              f"(saved about ${t['saved_prompt_cache']:.4f})",
              f"Local response cache: {t['local_hits']} calls answered without the API "
              f"(saved about ${t['saved_local']:.4f}); {s['local_cache_entries']} entries stored",
              f"HTTP cache: {s['http_cache_entries']} source responses stored"]
    return "\n".join(lines)
