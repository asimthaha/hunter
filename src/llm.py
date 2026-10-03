"""The ONLY module that calls the Anthropic API (plan.md §3, §12).

* Every request is serialised and checked against values from data/contact.yaml and
  data/profile.yaml before sending; a hit raises PIIGuardError and nothing is sent.
* Responses are forced through a single schema tool and validated; invalid output is retried
  once, then raises SchemaError (callers route the job to needs_review).
* Logs carry request IDs, token counts and job IDs — never request bodies.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import jsonschema

from . import config, db

log = logging.getLogger("hub.llm")

MIN_TEXT_LEN = 5        # shorter strings are too generic to guard (e.g. "INR", "yes")
MIN_DIGITS_SUBSTR = 7   # long digit strings (phones) are matched as a substring of the request's digits
MIN_DIGITS_RUN = 5      # shorter numbers (pay amounts) must match a whole digit run


class PIIGuardError(Exception):
    pass


class SchemaError(Exception):
    pass


class LLMError(Exception):
    pass


def _flatten(obj: Any) -> list[Any]:
    if isinstance(obj, dict):
        return [v for x in obj.values() for v in _flatten(x)]
    if isinstance(obj, (list, tuple)):
        return [v for x in obj for v in _flatten(x)]
    return [obj] if obj is not None else []


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


class PIIGuard:
    """Refuses requests containing any scalar value from contact.yaml / profile.yaml.

    Normalisation: text compared lower-cased (so emails match case-insensitively); numbers and
    phone-like values compared as digits only. Values too short to be identifying are exempt —
    store money as absolute annual amounts (e.g. 2500000, not 25) so they stay guardable.
    """

    def __init__(self, values: list[Any]):
        self.texts: set[str] = set()
        self.digit_strs: set[str] = set()
        self.digit_runs: set[str] = set()
        for v in values:
            if isinstance(v, bool):
                continue
            s = str(v).strip()
            d = _digits(s)
            if isinstance(v, (int, float)) or (d and len(d) >= len(s) * 0.6):
                if len(d) >= MIN_DIGITS_SUBSTR:
                    self.digit_strs.add(d)
                    if len(d) > 10:                 # phone with country code: also guard the national number
                        self.digit_strs.add(d[-10:])
                elif len(d) >= MIN_DIGITS_RUN:
                    self.digit_runs.add(d)
                continue
            if len(s) >= MIN_TEXT_LEN:
                self.texts.add(s.lower())
            if re.match(r"https?://", s, re.I):      # profile URLs: also guard the bare handle
                handle = s.rstrip("/").rsplit("/", 1)[-1]
                if len(handle) >= MIN_TEXT_LEN:
                    self.texts.add(handle.lower())
            if len(d) >= MIN_DIGITS_SUBSTR:
                self.digit_strs.add(d)
                if len(d) > 10:
                    self.digit_strs.add(d[-10:])

    @classmethod
    def from_files(cls) -> "PIIGuard":
        values: list[Any] = []
        for name in ("contact.yaml", "profile.yaml"):
            values += _flatten(config.load_yaml(name))
        return cls(values)

    def check(self, request_text: str) -> None:
        low = request_text.lower()
        for t in self.texts:
            if t in low:
                raise PIIGuardError("request contains a value from contact.yaml/profile.yaml; refusing to send")
        stream = _digits(request_text)
        for d in self.digit_strs:
            if d in stream:
                raise PIIGuardError("request contains a numeric value from contact.yaml/profile.yaml; refusing to send")
        if self.digit_runs:
            runs = set(re.findall(r"\d+", request_text.replace(",", "")))
            if runs & self.digit_runs:
                raise PIIGuardError("request contains a numeric value from profile.yaml; refusing to send")


def estimate_cost(model: str, inp: int, out: int, cache_read: int, cache_write: int) -> float:
    p = config.PRICES.get(model, {"in": 3.0, "out": 15.0})
    return (inp * p["in"] + out * p["out"]
            + cache_read * p["in"] * config.CACHE_READ_MULT
            + cache_write * p["in"] * config.CACHE_WRITE_MULT) / 1_000_000


class LLM:
    def __init__(self, client=None, conn=None, guard: PIIGuard | None = None):
        self._client = client
        self.conn = conn
        self.guard = guard if guard is not None else PIIGuard.from_files()

    @property
    def client(self):
        if self._client is None:
            import anthropic
            config.load_env()
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMError("ANTHROPIC_API_KEY is not set (see .env.example)")
            self._client = anthropic.Anthropic(max_retries=4, timeout=120.0)
        return self._client

    def call(self, *, stage: str, model: str, instructions: str, context: str = "", user: str,
             schema: dict, job_id: int | None = None, max_tokens: int = 2000) -> dict:
        """One schema-constrained call. `instructions` + `context` (e.g. fact bank) form the cached prefix."""
        system = [{"type": "text", "text": instructions}]
        if context:
            system.append({"type": "text", "text": context})
        system[-1]["cache_control"] = {"type": "ephemeral"}
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "submit", "description": "Submit the structured result.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "submit"},
        }
        self.guard.check(json.dumps(request, ensure_ascii=False))

        last_err = ""
        for attempt in (1, 2):
            try:
                resp = self.client.messages.create(**request)
            except Exception as e:  # SDK already retried rate-limit/overload with backoff
                raise LLMError(f"{type(e).__name__}: {e}") from e
            self._record(resp, stage, model, job_id)
            data = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            try:
                if data is None:
                    raise jsonschema.ValidationError("no tool_use block in response")
                jsonschema.validate(data, schema)
                return data
            except jsonschema.ValidationError as e:
                last_err = e.message[:200]
                log.warning("schema failure stage=%s job=%s attempt=%d", stage, job_id, attempt)
        raise SchemaError(f"response failed schema validation twice: {last_err}")

    def _record(self, resp, stage: str, model: str, job_id: int | None) -> None:
        u = getattr(resp, "usage", None)
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        rid = getattr(resp, "id", "")
        log.info("llm stage=%s model=%s job=%s id=%s in=%d out=%d cache_r=%d cache_w=%d",
                 stage, model, job_id, rid, inp, out, cr, cw)
        if self.conn is not None:
            self.conn.execute(
                """INSERT INTO llm_usage (at, job_id, stage, model, request_id, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (db.now(), job_id, stage, model, rid, inp, out, cr, cw, estimate_cost(model, inp, out, cr, cw)))
            self.conn.commit()
