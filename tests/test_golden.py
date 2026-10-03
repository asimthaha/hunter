"""Calibration regression (plan.md §6). Needs the real API: run `HUB_LIVE=1 pytest -m live`.
Case files: tests/golden/*.json -> {"label": "apply|maybe|skip", "job": {title, company, location_raw, description}}"""
import os

import pytest

from src import calibrate


def test_metrics_math():
    pairs = [("apply", "apply"), ("apply", "maybe"), ("skip", "apply"), ("skip", "skip"), ("apply", "review")]
    m = calibrate.metrics(pairs)
    assert (m["tp"], m["fn"], m["fp"]) == (1, 2, 1)
    assert m["recall"] == pytest.approx(1 / 3) and m["precision"] == pytest.approx(1 / 2)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("HUB_LIVE") != "1", reason="set HUB_LIVE=1 to call the API")
def test_apply_class_recall_on_golden_set():
    from src import config
    from src.llm import LLM
    config.load_env()
    if not calibrate.load_cases():
        pytest.skip("no golden cases yet — label >=50 postings first (plan.md §6)")
    m = calibrate.run(LLM())
    assert m["recall"] is not None and m["recall"] >= calibrate.TARGET_RECALL, m
