"""The ONLY module that calls the Anthropic API (plan.md §3, §12).

* Every request is serialised and checked against values from data/contact.yaml and
  data/profile.yaml before sending; a hit raises PIIGuardError and nothing is sent.
* Responses are constrained with structured outputs (`output_config.format`) and re-validated locally;
  invalid output is retried once, then raises SchemaError (callers route the job to needs_review).
  (Forced tool use is not used: claude-sonnet-5-5 rejects it with a 400.)
* Caching, two layers:
    1. Anthropic prompt caching: the fact bank is the first system block (shared by evaluate / tailor /
       drafts), stage instructions the second; the job posting is in the user message after both
       breakpoints. Cache read/write tokens are recorded per call.
    2. Local response cache (SQLite): an identical request is answered without an API call.
* Logs carry request IDs, token counts and job IDs — never request bodies; the local cache stores
  results only.
"""
from __future__ import annotations

import copy
import hashlib
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


# ------------------------------------------------------------------------------- cost / schema helpers
def estimate_cost(model: str, inp: int, out: int, cache_read: int, cache_write: int, write_1h: int = 0) -> float:
    """USD estimate. `cache_write` is total tokens written; `write_1h` of them used the 1-hour TTL (2x)."""
    p = config.PRICES.get(model, {"in": 2.0, "out": 10.0})
    write_1h = min(write_1h, cache_write)
    write_cost = ((cache_write - write_1h) * config.CACHE_WRITE_MULT + write_1h * config.CACHE_WRITE_MULT_1H) * p["in"]
    return (inp * p["in"] + out * p["out"] + cache_read * p["in"] * config.CACHE_READ_MULT + write_cost) / 1_000_000


_UNSUPPORTED_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
                         "maxLength", "pattern", "minItems", "maxItems", "uniqueItems")


def api_schema(schema: dict) -> dict:
    """Schema as sent to the API: structured outputs reject numeric/length constraints and need
    `additionalProperties: false` on every object, and nullable unions as `anyOf`. The ORIGINAL schema
    (with its constraints) is still used to validate every response locally."""
    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        node = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS or not isinstance(v, (int, float, str, bool))}
        t = node.get("type")
        if isinstance(t, list):
            rest = {k: v for k, v in node.items() if k != "type"}
            return {**rest, "anyOf": [{"type": x} for x in t]}
        if t == "object":
            node["additionalProperties"] = False
        return node
    return walk(copy.deepcopy(schema))


def cache_key(model: str, system_texts: list[str], user: str, schema: dict) -> str:
    blob = json.dumps({"m": model, "s": system_texts, "u": user, "schema": schema}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def prune_cache(conn, max_age_days: int | None = None) -> int:
    """Delete local response-cache entries older than the retention window."""
    from datetime import datetime, timedelta, timezone
    days = max_age_days if max_age_days is not None else config.LLM_CACHE_MAX_AGE_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    n = conn.execute("DELETE FROM llm_cache WHERE created_at < ?", (cutoff,)).rowcount
    conn.commit()
    return n


class LLM:
    def __init__(self, client=None, conn=None, guard: PIIGuard | None = None, use_cache: bool | None = None,
                 cache_ttl: str | None = None):
        self._client = client
        self.conn = conn
        self.guard = guard if guard is not None else PIIGuard.from_files()
        if use_cache is None:
            use_cache = config.LLM_CACHE_ENABLED and os.environ.get("HUB_LLM_CACHE", "1") != "0"
        self.use_cache = use_cache
        self.cache_ttl = cache_ttl or config.CACHE_TTL

    @property
    def client(self):
        if self._client is None:
            import anthropic
            config.load_env()
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMError("ANTHROPIC_API_KEY is not set (see .env.example)")
            self._client = anthropic.Anthropic(max_retries=4, timeout=120.0)
        return self._client

    # ---------------------------------------------------------------- request construction
    def _breakpoint(self) -> dict:
        cc = {"type": "ephemeral"}
        if self.cache_ttl == "1h":
            cc["ttl"] = "1h"
        return cc

    def build_request(self, *, model: str, instructions: str, context: str, user: str, schema: dict,
                      max_tokens: int) -> dict:
        """Prefix order = tools (none) -> system -> messages. The shared context (fact bank) comes first with
        its own breakpoint so every stage that uses it reads one cache entry; the stage instructions follow
        with a second breakpoint; the per-job text is in `messages`, after both."""
        system: list[dict] = []
        if context:
            system.append({"type": "text", "text": context, "cache_control": self._breakpoint()})
        system.append({"type": "text", "text": instructions, "cache_control": self._breakpoint()})
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": api_schema(schema)}},
        }

    # ---------------------------------------------------------------- the call
    def call(self, *, stage: str, model: str, instructions: str, context: str = "", user: str,
             schema: dict, job_id: int | None = None, max_tokens: int = 2000,
             use_cache: bool | None = None) -> dict:
        request = self.build_request(model=model, instructions=instructions, context=context, user=user,
                                     schema=schema, max_tokens=max_tokens)
        self.guard.check(json.dumps(request, ensure_ascii=False))       # always before anything else

        use_local = (self.use_cache if use_cache is None else use_cache) and self.conn is not None
        key = cache_key(model, [s["text"] for s in request["system"]], user, schema)
        if use_local:
            hit = self._local_lookup(key, schema)
            if hit is not None:
                self._record_local_hit(stage, model, job_id)
                return hit

        last_err = ""
        for attempt in (1, 2):
            try:
                resp = self.client.messages.create(**request)
            except Exception as e:  # SDK already retried rate-limit/overload with backoff
                raise LLMError(f"{type(e).__name__}: {e}") from e
            self._record(resp, stage, model, job_id)
            if getattr(resp, "stop_reason", None) == "refusal":
                raise SchemaError("model declined the request (refusal)")
            try:
                text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
                if not text:
                    raise jsonschema.ValidationError("no text block in response")
                data = json.loads(text)
                jsonschema.validate(data, schema)       # original schema, including numeric constraints
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                last_err = (getattr(e, "message", None) or str(e))[:200]
                log.warning("schema failure stage=%s job=%s attempt=%d stop=%s", stage, job_id, attempt,
                            getattr(resp, "stop_reason", None))
                continue
            if use_local:
                self._local_store(key, stage, model, data)
            return data
        raise SchemaError(f"response failed schema validation twice: {last_err}")

    # ---------------------------------------------------------------- local response cache
    def _local_lookup(self, key: str, schema: dict) -> dict | None:
        row = self.conn.execute("SELECT result_json FROM llm_cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["result_json"])
            jsonschema.validate(data, schema)
        except (ValueError, jsonschema.ValidationError):
            self.conn.execute("DELETE FROM llm_cache WHERE key=?", (key,))     # corrupt / schema changed
            self.conn.commit()
            return None
        self.conn.execute("UPDATE llm_cache SET hits=hits+1 WHERE key=?", (key,))
        self.conn.commit()
        return data

    def _local_store(self, key: str, stage: str, model: str, data: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO llm_cache (key, stage, model, result_json, created_at, hits) "
                          "VALUES (?,?,?,?,?,0)", (key, stage, model, json.dumps(data), db.now()))
        self.conn.commit()

    def _record_local_hit(self, stage: str, model: str, job_id: int | None) -> None:
        log.info("llm stage=%s model=%s job=%s LOCAL CACHE HIT (no API call)", stage, model, job_id)
        self.conn.execute(
            """INSERT INTO llm_usage (at, job_id, stage, model, request_id, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, cost_usd, local_cache_hit) VALUES (?,?,?,?,?,0,0,0,0,0,1)""",
            (db.now(), job_id, stage, model, "local-cache"))
        self.conn.commit()

    # ---------------------------------------------------------------- usage accounting
    def _record(self, resp, stage: str, model: str, job_id: int | None) -> None:
        u = getattr(resp, "usage", None)
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        breakdown = getattr(u, "cache_creation", None)
        w1h = getattr(breakdown, "ephemeral_1h_input_tokens", None)
        if w1h is None:
            w1h = cw if self.cache_ttl == "1h" else 0
        rid = getattr(resp, "id", "")
        log.info("llm stage=%s model=%s job=%s id=%s in=%d out=%d cache_r=%d cache_w=%d", stage, model, job_id, rid,
                 inp, out, cr, cw)
        if self.conn is not None:
            self.conn.execute(
                """INSERT INTO llm_usage (at, job_id, stage, model, request_id, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (db.now(), job_id, stage, model, rid, inp, out, cr, cw, estimate_cost(model, inp, out, cr, cw, w1h)))
            self.conn.commit()
