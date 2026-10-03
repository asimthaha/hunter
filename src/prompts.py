"""Prompts live in prompts/*.md. The first line is `<!-- version: N -->`; bump it on every change."""
from __future__ import annotations

import re

from . import config


def load(name: str) -> tuple[str, str]:
    text = (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"\s*<!--\s*version:\s*(\S+)\s*-->\s*", text)
    version = m.group(1) if m else "0"
    return version, text[m.end():] if m else text


def wrap_untrusted(text: str, label: str = "job_posting") -> str:
    """Wrap untrusted text in tags; neutralise any closing tag the posting itself contains."""
    safe = re.sub(r"</\s*untrusted[^>]*>", "[removed]", text, flags=re.I)
    return f"<untrusted_{label}>\n{safe}\n</untrusted_{label}>"
