"""Paths, model names and tunables. Modules read these as `config.X` at call time so tests can patch."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

CODE_ROOT = Path(__file__).resolve().parent.parent        # prompts/, templates/ ship with the code
ROOT = Path(os.environ.get("HUB_ROOT") or CODE_ROOT)       # HUB_ROOT relocates user data only
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
OUTPUT_DIR = ROOT / "output"
LOGS_DIR = ROOT / "logs"
PROMPTS_DIR = CODE_ROOT / "prompts"
TEMPLATES_DIR = CODE_ROOT / "templates"
GOLDEN_DIR = CODE_ROOT / "tests" / "golden"
DB_PATH = DATA_DIR / "jobs.db"

ELIGIBILITY_MODEL = "claude-haiku-4-5-20251001"
EVALUATE_MODEL = "claude-sonnet-5-5"
TAILOR_MODEL = "claude-sonnet-5-5"
DRAFT_MODEL = "claude-sonnet-5-5"

# Decision rule (plan.md §6). Implemented in code, tuned during calibration.
APPLY_MIN_SKILLS = 4
APPLY_MIN_SENIORITY = 3
APPLY_MAX_RED_FLAG = 1          # red_flags scale: 1 = none, 5 = severe
MAYBE_MIN_SKILLS = 3
MAYBE_MIN_SENIORITY = 2
MAYBE_MAX_RED_FLAG = 2

# Rough per-million-token USD prices, used for the spend *estimate* only. Verify against the console.
PRICES = {
    "claude-haiku-4-5-20251001": {"in": 1.0, "out": 5.0},
    "claude-sonnet-5-5": {"in": 3.0, "out": 15.0},
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25

# Approximate FX to USD, for pay comparison only.
FX_TO_USD = {"USD": 1.0, "INR": 0.012, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.65, "SGD": 0.74}

FETCH_TIMEOUT = 20.0
FETCH_MAX_BYTES = 2_000_000
SOURCE_RETRIES = 3

DEFAULT_PREFS = {
    "target_roles": [],
    "stack": "",
    "eligible_regions": ["india", "worldwide", "anywhere", "global", "apac", "asia"],
    "employment_types_excluded": [],
    "title_include_any": [],
    "title_exclude_any": [],
    "seniority_exclude_any": ["intern", "internship", "trainee"],
    "pay_suspicious_usd": 400000,
    "resume_max_pages": 2,
    "company_cooldown_days": 14,
    "follow_up_days": 10,
    "audit_sample_size": 10,
    "jobspy": {"searches": [], "results_wanted": 20},
}


def load_env() -> None:
    """Load KEY=VALUE pairs from .env into os.environ (explicitly, so scheduled tasks work)."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_yaml(name: str) -> dict:
    path = DATA_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_prefs() -> dict:
    prefs = dict(DEFAULT_PREFS)
    prefs.update(load_yaml("preferences.yaml"))
    return prefs
