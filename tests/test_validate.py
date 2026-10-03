from src import facts as factsmod
from src.validate import apply_substitutions, validate_draft, validate_resume

import yaml
from pathlib import Path

BANK = factsmod.parse(yaml.safe_load((Path(__file__).parent.parent / "data" / "facts.example.yaml").read_text()))


def sel(**kw):
    base = {"summary_id": "sum-backend", "variant_ids": ["acme-1a", "acme-2a"], "skills": ["Python"],
            "substitutions": [], "missing_evidence": []}
    base.update(kw)
    return base


def test_valid_selection_passes():
    assert validate_resume(sel(), BANK) == []


def test_unknown_ids_rejected():
    assert validate_resume(sel(variant_ids=["nope-1a"]), BANK)
    assert validate_resume(sel(summary_id="nope"), BANK)


def test_at_most_one_variant_per_fact():
    errs = validate_resume(sel(variant_ids=["acme-1a", "acme-1b"]), BANK)
    assert any("more than one variant" in e for e in errs)


def test_duplicate_variant_rejected():
    assert validate_resume(sel(variant_ids=["acme-1a", "acme-1a"]), BANK)


def test_skill_must_be_in_bank():
    assert validate_resume(sel(skills=["Kafka"]), BANK)


def test_substitutions_only_from_synonym_map():
    assert validate_resume(sel(substitutions=[{"from": "ETL", "to": "data pipeline"}]), BANK) == []
    assert validate_resume(sel(substitutions=[{"from": "ETL", "to": "big data platform"}]), BANK)


def test_substitution_is_whole_word_and_case_preserving():
    subs = [{"from": "ETL", "to": "data pipeline"}]
    assert apply_substitutions("Built a Python ETL pipeline", subs) == "Built a Python data pipeline pipeline"
    assert apply_substitutions("NETLIFY etl tool", subs) == "NETLIFY data pipeline tool"
    assert apply_substitutions("Data work", [{"from": "data", "to": "records"}]) == "Records work"


def test_certifications_and_education_can_be_skill_evidence():
    raw = {"version": 1, "summaries": [{"id": "s", "text": "x"}], "roles": [],
           "certifications": [{"id": "cert-aws", "text": "AWS Cloud Technical Essentials"}],
           "education": [{"id": "edu-1", "text": "MCA"}],
           "skills": [{"name": "AWS", "evidence": ["cert-aws"]}, {"name": "Ghost", "evidence": ["cert-nope"]}]}
    bank = factsmod.parse(raw)
    ok = {"summary_id": "s", "variant_ids": [], "skills": ["AWS"], "substitutions": []}
    assert validate_resume(ok, bank) == []
    assert validate_resume({**ok, "skills": ["Ghost"]}, bank)          # unknown evidence id is rejected
    assert "AWS Cloud Technical Essentials" in bank.for_prompt()
    assert validate_draft([{"text": "I completed AWS Cloud Technical Essentials.", "fact_ids": ["cert-aws"]}], bank) == []


# ---- drafts
def good(**kw):
    s = {"text": "I built a Python ETL pipeline processing 2M records/day.", "fact_ids": ["acme-1a"]}
    s.update(kw)
    return [s]


def test_grounded_draft_passes():
    assert validate_draft(good(), BANK, ["Globex", "Backend Engineer"]) == []


def test_sentence_without_citation_rejected():
    assert validate_draft(good(fact_ids=[]), BANK)


def test_unknown_fact_id_rejected():
    assert validate_draft(good(fact_ids=["zzz"]), BANK)


def test_invented_number_rejected():
    assert validate_draft(good(text="I built a Python pipeline processing 5M records/day."), BANK)


def test_uncited_technology_rejected():
    errs = validate_draft(good(text="I built APIs with FastAPI."), BANK)    # FastAPI only in acme-2a
    assert any("FastAPI" in e for e in errs)


def test_credential_rejected():
    assert validate_draft(good(text="I am a certified Python engineer."), BANK)


def test_unknown_proper_noun_rejected_but_job_company_allowed():
    assert validate_draft(good(text="I worked with Kubernetes at Google."), BANK, ["Globex"])
    assert validate_draft(good(text="I would love to join Globex."), BANK, ["Globex"]) == []
