"""Anti-fabrication validators (plan.md §9, §13). Pure functions; no I/O, no model calls.

* validate_resume: model-selected IDs must exist, one variant per parent fact, skills have evidence,
  substitutions come only from synonym_map.
* validate_draft: every sentence cites fact IDs, and any number / known entity / credential /
  proper noun in it must appear in the cited facts (or be job context supplied by code).
"""
from __future__ import annotations

import re
from collections import Counter

from .facts import FactBank


# ----------------------------------------------------------------------------- resume
def validate_resume(sel: dict, bank: FactBank) -> list[str]:
    errors: list[str] = []
    if sel.get("summary_id") not in bank.summaries:
        errors.append(f"unknown summary id: {sel.get('summary_id')!r}")
    variant_ids = sel.get("variant_ids", [])
    for vid, n in Counter(variant_ids).items():
        if n > 1:
            errors.append(f"variant {vid} selected more than once")
    parents: Counter = Counter()
    for vid in set(variant_ids):
        if vid not in bank.variants:
            errors.append(f"unknown variant id: {vid!r}")
        else:
            parents[bank.variants[vid]["parent"]] += 1
    for parent, n in parents.items():
        if n > 1:
            errors.append(f"more than one variant of fact {parent} selected")
    known = bank.known_ids()
    for name in sel.get("skills", []):
        skill = bank.skills.get(name.lower())
        if skill is None:
            errors.append(f"skill {name!r} is not in the fact bank")
            continue
        ev = skill.get("evidence") or []
        if not ev:
            errors.append(f"skill {name!r} has no evidence in the fact bank")
        elif any(e not in known for e in ev):
            errors.append(f"skill {name!r} cites unknown evidence ids")
    for sub in sel.get("substitutions", []):
        pair = (str(sub.get("from", "")).lower(), str(sub.get("to", "")).lower())
        if pair not in bank.synonyms:
            errors.append(f"substitution {sub.get('from')!r} -> {sub.get('to')!r} is not in synonym_map")
    return errors


def apply_substitutions(text: str, subs: list[dict]) -> str:
    """Whole-word, case-preserving replacement of pre-approved synonym pairs."""
    for sub in subs:
        src, dst = sub["from"], sub["to"]

        def repl(m: re.Match, dst=dst) -> str:
            w = m.group(0)
            if len(w) > 1 and w.isupper() and not dst.isupper():
                return dst                       # acronym -> phrase: keep the approved spelling
            if w[:1].isupper():
                return dst[:1].upper() + dst[1:]
            return dst

        text = re.sub(rf"(?<!\w){re.escape(src)}(?!\w)", repl, text, flags=re.I)
    return text


# ----------------------------------------------------------------------------- drafts
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CREDENTIAL = re.compile(r"\b(certified|certification|certificate|degree|ph\.?d|master'?s|bachelor'?s|mba|b\.?tech|m\.?tech|diploma)\b", re.I)
_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+#.'-]*")
_STOP = {"i", "i'm", "i've", "i'd", "i'll", "hi", "hello", "hey", "thanks", "thank", "dear", "best", "regards", "ai",
         "monday", "tuesday", "wednesday", "thursday", "friday"}


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(phrase.lower())}(?![A-Za-z0-9])", text.lower()) is not None


def _entities(bank: FactBank) -> set[str]:
    ents = {s["name"] for s in bank.skills.values()}
    ents |= {r.get("company", "") for r in bank.roles.values()}
    return {e for e in ents if e}


def validate_draft(sentences: list[dict], bank: FactBank, context_terms: list[str] | None = None) -> list[str]:
    """`context_terms`: strings from the job record (company, title) that code — not the model — supplies."""
    errors: list[str] = []
    ctx = " ".join(context_terms or [])
    entities = _entities(bank)
    if not sentences:
        return ["draft has no sentences"]
    for i, s in enumerate(sentences, 1):
        text, ids = s.get("text", ""), s.get("fact_ids") or []
        if not ids:
            errors.append(f"sentence {i}: cites no facts")
            continue
        unknown = [x for x in ids if x not in bank.known_ids() and x not in bank.roles]
        if unknown:
            errors.append(f"sentence {i}: unknown fact ids {unknown}")
            continue
        cited = " ".join(bank.text_of(x) for x in ids)
        cited_nums = {n.rstrip(".,").replace(",", "") for n in _NUM.findall(cited)}
        for n in _NUM.findall(text):
            if n.rstrip(".,").replace(",", "") not in cited_nums and not _has(ctx, n):
                errors.append(f"sentence {i}: number {n!r} not in cited facts")
        for ent in entities:
            if _has(text, ent) and not _has(cited, ent) and not _has(ctx, ent):
                errors.append(f"sentence {i}: {ent!r} not in cited facts")
        for m in _CREDENTIAL.finditer(text):
            if not _has(cited, m.group(0)):
                errors.append(f"sentence {i}: credential {m.group(0)!r} not in cited facts")
        words = list(_WORD.finditer(text))
        for j, m in enumerate(words):
            w = m.group(0).rstrip(".'-")
            if j == 0 or not w[0].isupper() or w.lower() in _STOP:
                continue
            prev_end = text[:m.start()].rstrip()
            if prev_end.endswith((".", "!", "?")):      # sentence-initial after punctuation
                continue
            if not _has(cited, w) and not _has(ctx, w):
                errors.append(f"sentence {i}: {w!r} not in cited facts")
    return sorted(set(errors))
