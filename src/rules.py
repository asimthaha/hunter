"""Deterministic rules (plan.md §7, §12). They run in code, independent of the model, so a posting
cannot talk its way past them. Reads profile.yaml (pay floor, notice) locally; never sent anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config


@dataclass
class Flag:
    kind: str
    action: str          # reject | review
    reason: str
    quote: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "action": self.action, "reason": self.reason, "quote": self.quote}


@dataclass
class RuleResult:
    decision: str = "pass"   # pass | reject | review
    flags: list[Flag] = field(default_factory=list)

    def add(self, flag: Flag | None) -> None:
        if flag is None:
            return
        self.flags.append(flag)
        if flag.action == "reject":
            self.decision = "reject"
        elif self.decision != "reject":
            self.decision = "review"

    @property
    def reason(self) -> str:
        return "; ".join(f"{f.kind}: {f.reason}" for f in self.flags)


def _snippet(text: str, m: re.Match, pad: int = 40) -> str:
    return re.sub(r"\s+", " ", text[max(0, m.start() - pad): m.end() + pad]).strip()


# --- scam patterns -----------------------------------------------------------------------------
_NEGATION = re.compile(r"\b(no|not|never|without|zero|free of|nor)\b[^.\n]{0,25}$", re.I)
_MONEY = [
    re.compile(r"\b(registration|training|security|refundable|processing|joining|application|onboarding|"
               r"verification|interview|kit|laptop)\s+(fee|fees|deposit|charges?|amount)\b", re.I),
    re.compile(r"\b(pay|deposit|transfer|send|remit)\b[^.\n]{0,40}\b(fee|deposit|amount|money)\b"
               r"[^.\n]{0,40}\b(before|to (start|join|secure|confirm)|for (the )?(laptop|equipment|kit))", re.I),
    re.compile(r"\b(equipment|laptop)\s+(cheque|check)\b", re.I),
    re.compile(r"\b(cheque|check)\b[^.\n]{0,30}\b(equipment|laptop|purchase)\b", re.I),
]
_MESSAGING = re.compile(r"\b(telegram|whatsapp|signal)\b", re.I)
_ID_REQUEST = re.compile(
    r"\b(aadhaar|aadhar|bank (account )?(details|number|statement)|pan\s+(card|number|no\.?|details)|"
    r"passport (copy|scan)|id (scan|proof copy)|ssn|social security number)\b", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
FREE_EMAIL_DOMAINS = {"gmail.com", "outlook.com", "yahoo.com", "yahoo.in", "hotmail.com", "rediffmail.com",
                      "proton.me", "protonmail.com", "icloud.com", "live.com", "aol.com"}
ATS_EMAIL_DOMAINS = {"greenhouse.io", "lever.co", "ashbyhq.com", "workday.com", "myworkday.com", "smartrecruiters.com"}


_COMPANY_STOP = {"pvt", "ltd", "limited", "private", "technologies", "technology", "solutions", "systems", "software",
                 "services", "infotech", "global", "india", "innovations", "labs", "group", "corp", "company",
                 "the", "and", "inc", "llc", "llp", "digital", "info", "soft"}
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def _domain_matches(domain: str, slug: str, tokens: list[str]) -> bool:
    """Company legal names ('SubHosting Innovations (P) Ltd') differ from mail domains (subhosting.net):
    match on any distinctive name token appearing in the domain, or the domain's main label in the name."""
    parts = domain.split(".")
    core = parts[-3] if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL else (parts[-2] if len(parts) >= 2 else domain)
    dslug = re.sub(r"[^a-z0-9]", "", domain)
    return slug in dslug or any(t in dslug for t in tokens) or (len(core) >= 4 and core in slug)


def scan_scam(text: str, company: str = "") -> list[Flag]:
    flags: list[Flag] = []
    for pat in _MONEY:
        for m in pat.finditer(text):
            if _NEGATION.search(text[max(0, m.start() - 30): m.start()]):
                continue
            flags.append(Flag("asks_for_money", "reject", "posting asks for money", _snippet(text, m)))
            break
    if flags:
        pass  # one money flag is enough
    m = _MESSAGING.search(text)
    if m:
        flags.append(Flag("off_platform_contact", "review", "contact/interview via Telegram/WhatsApp/Signal",
                          _snippet(text, m)))
    m = _ID_REQUEST.search(text)
    if m:
        flags.append(Flag("id_request", "reject", "asks for ID / bank details", _snippet(text, m)))
    slug = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    tokens = [t for t in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(t) >= 3 and t not in _COMPANY_STOP]
    for m in _EMAIL.finditer(text):
        domain = m.group(1).lower()
        if domain in FREE_EMAIL_DOMAINS:
            flags.append(Flag("free_email_recruiter", "review", f"recruiter contact on free domain {domain}",
                              _snippet(text, m)))
            break
        if slug and not _domain_matches(domain, slug, tokens) and not any(domain.endswith(a) for a in ATS_EMAIL_DOMAINS):
            flags.append(Flag("contact_domain_mismatch", "review",
                              f"contact domain {domain} doesn't match company", _snippet(text, m)))
            break
    return flags


# --- prompt-injection tells --------------------------------------------------------------------
_INJECTION = [
    re.compile(r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)", re.I),
    re.compile(r"disregard\b[^.\n]{0,30}\b(instructions?|prompts?|rules?)", re.I),
    re.compile(r"\byou\s+are\s+(now\s+)?(an?\s+)?(ai|language model|llm|assistant|chatbot|claude)\b", re.I),
    re.compile(r"\b(rate|score|rank|grade)\s+(this|the)\s+(candidate|applicant|resume|job|role|fit)\b", re.I),
    re.compile(r"\bas an ai\b|\bsystem prompt\b|\bnew instructions?\b", re.I),
    re.compile(r"\b(5/5|10/10|perfect score|maximum score)\b", re.I),
]
_HIDDEN_CHARS = re.compile("[​-‏⁠﻿‪-‮⁦-⁩]")


def scan_injection(text: str) -> list[Flag]:
    flags: list[Flag] = []
    for pat in _INJECTION:
        m = pat.search(text)
        if m:
            flags.append(Flag("injection_suspected", "review", "instruction-like text in posting", _snippet(text, m)))
            break
    if _HIDDEN_CHARS.search(text):
        flags.append(Flag("hidden_characters", "review", "zero-width/bidi characters in posting"))
    return flags


# --- title / location / notice -----------------------------------------------------------------
def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word.lower())}(?!\w)", text.lower()) is not None


def check_title(title: str, prefs: dict) -> Flag | None:
    for w in prefs.get("seniority_exclude_any", []) + prefs.get("title_exclude_any", []):
        if _has_word(title, w):
            return Flag("title_excluded", "reject", f"title contains '{w}'", title)
    inc = prefs.get("title_include_any", [])
    if inc and not any(_has_word(title, w) for w in inc):
        return Flag("title_no_match", "reject", "title matches none of title_include_any", title)
    return None


_RESTRICTIVE = re.compile(
    r"\b(us|u\.s\.|usa|united states|canada|uk|united kingdom|eu|europe)\b[\s,/-]{0,3}"
    r"(only|based only|residents? only|citizens? only)\b|"
    r"\bmust (be )?(located|based|reside|living)\s+in\s+(the\s+)?(us|usa|united states|canada|uk|europe|eu)\b", re.I)
_OPEN_WORDS = re.compile(r"\b(india|worldwide|anywhere|global|apac|asia)\b", re.I)


def check_location(location: str, text: str = "") -> Flag | None:
    """Reject only clear region locks; 'US or India' / 'US only (India ok)' must pass."""
    for hay in (location or "", (text or "")[:3000]):
        for m in _RESTRICTIVE.finditer(hay):
            ctx = hay[max(0, m.start() - 60): m.end() + 60]
            if _OPEN_WORDS.search(ctx):
                continue
            return Flag("location_locked", "reject", "region-locked posting", _snippet(hay, m))
    return None


_NOTICE_IMMEDIATE = re.compile(r"\bimmediate\s+(joiners?|joining)\b[^.\n]{0,25}\bonly\b|"
                               r"\bonly\b[^.\n]{0,25}\bimmediate\s+(joiners?|joining)\b|"
                               r"\bmust\b[^.\n]{0,20}\bjoin\s+immediately\b", re.I)
_NOTICE_DAYS = re.compile(r"\b(join(ing)?|notice(\s+period)?)\b[^.\n]{0,30}\b(within|max(imum)?(\s+of)?|up\s*to|not\s+more\s+than|"
                          r"less\s+than|at\s+most)\s*(\d{1,3})\s*days\b", re.I)


def required_notice_days(text: str) -> tuple[int, str] | None:
    m = _NOTICE_IMMEDIATE.search(text)
    if m:
        return 0, _snippet(text, m)
    m = _NOTICE_DAYS.search(text)
    if m:
        return int(m.group(m.lastindex)), _snippet(text, m)
    return None


def check_notice(max_days: int | None, profile: dict, quote: str = "") -> Flag | None:
    mine = profile.get("notice_days")
    if max_days is None or mine is None:
        return None
    if int(mine) > int(max_days):
        return Flag("notice_incompatible", "reject", f"posting needs joining within {max_days} days", quote)
    return None


def apply_rules(job: dict, prefs: dict, profile: dict) -> RuleResult:
    """Run every deterministic rule on a job dict (title, company, location_raw, description)."""
    res = RuleResult()
    text = job.get("description") or ""
    res.add(check_title(job.get("title") or "", prefs))
    res.add(check_location(job.get("location_raw") or "", text))
    for f in scan_scam(text, job.get("company") or ""):
        res.add(f)
    for f in scan_injection(text):
        res.add(f)
    req = required_notice_days(text)
    if req:
        res.add(check_notice(req[0], profile, req[1]))
    return res


# --- pay (plan.md §6: compared in code, never sent to the API) ---------------------------------
PERIOD_TO_ANNUAL = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080, "lakh_per_annum": 100_000}


def annualize(amount: float, period: str) -> float | None:
    mult = PERIOD_TO_ANNUAL.get(period)
    return amount * mult if mult else None


def to_currency(amount: float, src: str, dst: str) -> float | None:
    fx = config.FX_TO_USD
    if src not in fx or dst not in fx:
        return None
    return amount * fx[src] / fx[dst]


def pay_status(pay: dict | None, bucket: str | None, profile: dict) -> str:
    """not_stated | below_min | ok | unknown. `not_stated` is NOT below-minimum."""
    if not pay or not pay.get("stated") or pay.get("amount_min") is None and pay.get("amount_max") is None:
        return "not_stated"
    floor = (profile.get("min_pay") or {}).get(bucket or "")
    if not floor:
        return "unknown"
    amount = pay.get("amount_max") if pay.get("amount_max") is not None else pay.get("amount_min")
    annual = annualize(float(amount), pay.get("period") or "year")
    cur = (pay.get("currency") or "").upper()
    if annual is None or not cur:
        return "unknown"
    # lakh_per_annum implies INR
    cur = "INR" if pay.get("period") == "lakh_per_annum" else cur
    comparable = to_currency(annual, cur, floor["currency"].upper())
    if comparable is None:
        return "unknown"
    return "below_min" if comparable < float(floor["amount"]) else "ok"


def pay_suspiciously_high(pay: dict | None, prefs: dict) -> Flag | None:
    if not pay or not pay.get("stated"):
        return None
    amount = pay.get("amount_min") if pay.get("amount_min") is not None else pay.get("amount_max")
    if amount is None:
        return None
    annual = annualize(float(amount), pay.get("period") or "year")
    cur = "INR" if pay.get("period") == "lakh_per_annum" else (pay.get("currency") or "").upper()
    usd = to_currency(annual, cur, "USD") if annual is not None else None
    if usd is not None and usd > prefs.get("pay_suspicious_usd", 400000):
        return Flag("pay_suspicious", "review", "stated pay far above market", pay.get("quote", ""))
    return None
