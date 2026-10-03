"""Resume rendering (plan.md §9). The template only ever receives fact-bank text and contact.yaml
values — never job-posting text. Jinja autoescape is on; Playwright runs with JS disabled and
all network requests blocked.
"""
from __future__ import annotations

import copy
import io
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import config


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(config.TEMPLATES_DIR)), autoescape=True)


def contact_line(contact: dict) -> str:
    parts = [contact.get("email"), contact.get("phone"), contact.get("location") or contact.get("address")]
    parts += list(contact.get("links", []) or [])
    return " | ".join(str(p) for p in parts if p)


def build_html(model: dict, contact: dict) -> str:
    return _env().get_template("resume.html").render(
        name=contact.get("name", ""), contact_line=contact_line(contact), summary=model["summary"],
        skills=model["skills"], roles=model["roles"], education=model.get("education", []))


def drop_lowest(model: dict) -> dict | None:
    """Remove the lowest-ranked bullet still present; None if nothing left to drop (keep >=1 bullet)."""
    m = copy.deepcopy(model)
    total = sum(len(r["bullets"]) for r in m["roles"])
    if total <= 1:
        return None
    for vid in reversed(m["rank"]):
        for role in m["roles"]:
            if any(b["id"] == vid for b in role["bullets"]):
                role["bullets"] = [b for b in role["bullets"] if b["id"] != vid]
                m["roles"] = [r for r in m["roles"] if r["bullets"]]
                m["rank"] = [x for x in m["rank"] if x != vid]
                return m
    return None


def _pdf_bytes(html: str) -> bytes:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(java_script_enabled=False)
            page = ctx.new_page()
            page.route("**/*", lambda route: route.abort())   # no network
            page.set_content(html)
            return page.pdf(format="A4", print_background=False)
        finally:
            browser.close()


def pdf_text(pdf: bytes) -> tuple[int, str]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf))
    return len(reader.pages), "\n".join(p.extract_text() or "" for p in reader.pages)


def _squash(s: str) -> str:
    return re.sub(r"\s+|-(?=\s)", "", s).casefold()


def expected_strings(model: dict, contact: dict) -> list[str]:
    out = [model["summary"]]
    for r in model["roles"]:
        out += [r["title"], r["company"], r["dates"]] + [b["text"] for b in r["bullets"]]
    out += model["skills"]
    return out


def missing_from_pdf(model: dict, contact: dict, text: str) -> list[str]:
    hay = _squash(text)
    return [s for s in expected_strings(model, contact) if _squash(s) not in hay]


def render_resume(model: dict, contact: dict, out_path: Path, max_pages: int | None = None) -> dict:
    """Render, shrink to the page limit by dropping lowest-ranked bullets, then verify the PDF text.
    Returns {path, pages, dropped, parse_problems}."""
    max_pages = max_pages or config.load_prefs()["resume_max_pages"]
    dropped: list[str] = []
    current = model
    while True:
        pdf = _pdf_bytes(build_html(current, contact))
        pages, text = pdf_text(pdf)
        if pages <= max_pages:
            break
        nxt = drop_lowest(current)
        if nxt is None:
            break
        dropped.append(current["rank"][-1])
        current = nxt
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pdf)
    return {"path": str(out_path), "pages": pages, "dropped": dropped,
            "parse_problems": missing_from_pdf(current, contact, text), "over_limit": pages > max_pages}
