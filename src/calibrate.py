"""Calibration against tests/golden/ through the real pipeline code (rules + prompts + models).
Measures precision and recall on the `apply` class (plan.md §6). No database writes."""
from __future__ import annotations

import json

from . import config, evaluate, facts as factsmod, rules
from .llm import LLM

TARGET_RECALL, TARGET_PRECISION = 0.85, 0.70


def load_cases() -> list[dict]:
    cases = []
    for p in sorted(config.GOLDEN_DIR.glob("*.json")):
        c = json.loads(p.read_text(encoding="utf-8"))
        c["name"] = p.name
        cases.append(c)
    return cases


def predict(llm: LLM, job: dict, bank, prefs, profile) -> str:
    res = rules.apply_rules(job, prefs, profile)
    if res.decision == "reject":
        return "skip"
    if res.decision == "review":
        return "review"
    out = evaluate.assess(llm, job, bank, prefs, profile)
    return out["decision"]


def metrics(pairs: list[tuple[str, str]]) -> dict:
    """pairs of (label, predicted). `review` predictions count as not-apply (they are a human queue)."""
    tp = sum(1 for l, p in pairs if l == "apply" and p == "apply")
    fn = sum(1 for l, p in pairs if l == "apply" and p != "apply")
    fp = sum(1 for l, p in pairs if l != "apply" and p == "apply")
    return {"n": len(pairs), "apply_labeled": tp + fn, "tp": tp, "fn": fn, "fp": fp,
            "recall": tp / (tp + fn) if tp + fn else None, "precision": tp / (tp + fp) if tp + fp else None}


def run(llm: LLM) -> dict:
    bank, prefs, profile = factsmod.load(), config.load_prefs(), config.load_yaml("profile.yaml")
    cases = load_cases()
    pairs, misses = [], []
    for c in cases:
        pred = predict(llm, c["job"], bank, prefs, profile)
        pairs.append((c["label"], pred))
        if c["label"] == "apply" and pred != "apply":
            misses.append({"case": c["name"], "predicted": pred})
    m = metrics(pairs)
    m["false_negatives"] = misses
    m["go"] = bool(m["recall"] is not None and m["precision"] is not None
                   and m["recall"] >= TARGET_RECALL and m["precision"] >= TARGET_PRECISION and m["n"] >= 50)
    return m
