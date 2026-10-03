import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, db

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """Isolated data dir populated from the *.example.yaml files (never real personal data)."""
    data = tmp_path / "data"
    data.mkdir()
    for ex in (REPO / "data").glob("*.example.yaml"):
        shutil.copy(ex, data / ex.name.replace(".example", ""))
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "jobs.db")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "GOLDEN_DIR", tmp_path / "golden")
    return SimpleNamespace(path=tmp_path, data=data)


@pytest.fixture
def conn(hub):
    c = db.connect()
    yield c
    c.close()


def make_job(**kw):
    base = dict(source="test", external_id="1", company="Acme", company_canonical="acme",
                title="Backend Engineer", location_raw="Remote - India", location_norm="remote india",
                url="https://example.com/job/1", description="Build Python services. Remote from India.",
                description_hash="h1", posted_at=None)
    base.update(kw)
    return base


class FakeLLM:
    """Stands in for src.llm.LLM in pipeline tests; returns scripted results per stage."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, *, stage, **kw):
        self.calls.append(stage)
        r = self.responses[stage]
        return r(kw) if callable(r) else r


def eligibility_ok(**over):
    ext = {
        "allowed_regions": {"value": ["india"], "quote": ""},
        "timezone_overlap": {"value": "not stated", "quote": ""},
        "work_authorization": {"value": "not_stated", "quote": ""},
        "employment_type": {"value": "employee", "quote": ""},
        "notice_requirement": {"max_days": None, "quote": ""},
        "pay": {"stated": False, "amount_min": None, "amount_max": None, "currency": None,
                "period": "not_stated", "quote": ""},
        "language": "English",
        "asks_for_money": {"value": False, "quote": ""},
    }
    ext.update(over)
    return ext


def evaluation(skills=5, seniority=4, domain=4, tz=5, red=1, bucket="india", quote=""):
    d = lambda s: {"score": s, "reason": "r", "quote": quote}
    return {"skills_match": d(skills), "seniority_fit": d(seniority), "domain": d(domain),
            "timezone_overlap": d(tz), "red_flags": d(red), "bucket": bucket, "summary": "ok"}
