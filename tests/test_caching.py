"""Prompt caching, local response cache, HTTP cache, structured-output requests and usage reporting."""
import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from src import config, db, digest, drafts, evaluate, facts as factsmod, ingest, llm as llmmod, tailor, usage
from src.llm import LLM, PIIGuard, PIIGuardError, SchemaError
from tests.conftest import FakeLLM, make_job

SCHEMA = {"type": "object", "properties": {
    "score": {"type": "integer", "minimum": 1, "maximum": 5},
    "note": {"type": ["string", "null"]},
    "inner": {"type": "object", "properties": {"a": {"type": "number", "minimum": 0}}, "required": ["a"]}},
    "required": ["score", "note", "inner"]}
GOOD = {"score": 3, "note": None, "inner": {"a": 1}}


class Client:
    """Scripted Anthropic client: returns queued responses, records every request."""
    def __init__(self, *responses):
        self.queue, self.sent = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.sent.append(req)
        r = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return r


def reply(data=None, *, text=None, stop="end_turn", cr=0, cw=0, inp=100, out=20, w1h=None):
    block = SimpleNamespace(type="text", text=text if text is not None else json.dumps(data))
    usage_ = SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cr,
                             cache_creation_input_tokens=cw,
                             cache_creation=None if w1h is None else SimpleNamespace(ephemeral_1h_input_tokens=w1h))
    return SimpleNamespace(content=[block], id="req_x", stop_reason=stop, usage=usage_)


def call(l, user="posting text", context="FACTS " * 50, instructions="Do the thing", **kw):
    return l.call(stage="evaluate", model="claude-sonnet-5-5", instructions=instructions, context=context,
                  user=user, schema=SCHEMA, **kw)


# ---------------------------------------------------------------- request layout
def test_request_layout_for_prompt_caching(hub):
    l = LLM(client=Client(reply(GOOD)), guard=PIIGuard([]))
    req = l.build_request(model="m", instructions="INSTR", context="CONTEXT", user="JOB", schema=SCHEMA, max_tokens=99)
    assert [b["text"] for b in req["system"]] == ["CONTEXT", "INSTR"]           # shared context first
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in req["system"])
    assert "cache_control" not in json.dumps(req["messages"])                    # per-job text is after the breakpoints
    assert req["messages"] == [{"role": "user", "content": "JOB"}]
    assert "tools" not in req and "tool_choice" not in req                       # forced tool use 400s on sonnet-5-5
    assert req["output_config"]["format"]["type"] == "json_schema"


def test_no_context_means_single_breakpoint_and_1h_ttl_is_applied(hub):
    l = LLM(client=Client(reply(GOOD)), guard=PIIGuard([]), cache_ttl="1h")
    req = l.build_request(model="m", instructions="INSTR", context="", user="J", schema=SCHEMA, max_tokens=9)
    assert len(req["system"]) == 1 and req["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_prefix_is_deterministic_across_calls(hub):
    l = LLM(client=Client(reply(GOOD)), guard=PIIGuard([]))
    a = l.build_request(model="m", instructions="I", context="C", user="job one", schema=SCHEMA, max_tokens=9)
    b = l.build_request(model="m", instructions="I", context="C", user="job two", schema=SCHEMA, max_tokens=9)
    assert json.dumps(a["system"], sort_keys=True) == json.dumps(b["system"], sort_keys=True)   # only `messages` differs


def test_all_sonnet_stages_share_one_cached_context(conn, monkeypatch):
    bank, prefs = factsmod.load(), config.load_prefs()
    contexts = {}

    def grab(stage, payload):
        def fn(kw):
            contexts[stage] = kw["context"]
            return payload
        return fn
    job_id = db.insert_job(conn, make_job())
    job = db.get_job(conn, job_id)
    evaluate.extract_evaluation(FakeLLM({"evaluate": grab("evaluate", {})}), job, bank, prefs)
    for s in ("scored", "shortlisted"):
        db.transition(conn, job_id, s, "t")
    tailor.tailor_job(conn, FakeLLM({"tailor": grab("tailor", {"summary_id": "x", "variant_ids": [], "skills": [],
                                                                 "substitutions": [], "missing_evidence": []})}), job_id)
    drafts.generate(conn, FakeLLM({"drafts": grab("drafts", {"drafts": []})}), job_id)
    assert len(set(contexts.values())) == 1 and set(contexts) == {"evaluate", "tailor", "drafts"}
    assert "FACT BANK" in contexts["evaluate"] and "CANDIDATE PREFERENCES" in contexts["evaluate"]


# ---------------------------------------------------------------- schema handling
def test_api_schema_strips_unsupported_and_normalises_but_original_is_untouched():
    original = copy.deepcopy(SCHEMA)
    s = llmmod.api_schema(SCHEMA)
    assert SCHEMA == original                                               # not mutated
    text = json.dumps(s)
    assert "minimum" not in text and "maximum" not in text
    assert s["additionalProperties"] is False and s["properties"]["inner"]["additionalProperties"] is False
    assert s["properties"]["note"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


@pytest.mark.parametrize("schema", [evaluate.ELIGIBILITY_SCHEMA, evaluate.EVALUATE_SCHEMA, tailor.TAILOR_SCHEMA,
                                    drafts.DRAFT_SCHEMA])
def test_real_schemas_require_every_property_and_convert_cleanly(schema):
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node.get("required", [])) == set(node["properties"]), node
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    check(schema)
    api = json.dumps(llmmod.api_schema(schema))
    assert '"minimum"' not in api and '"maximum"' not in api and '["integer", "null"]' not in api


def test_out_of_range_value_is_caught_locally_even_though_api_schema_has_no_bounds(hub):
    bad = {**GOOD, "score": 9}
    c = Client(reply(bad))
    with pytest.raises(SchemaError):
        call(LLM(client=c, guard=PIIGuard([])))
    assert len(c.sent) == 2                                                  # retried once


def test_invalid_json_retries_once_then_succeeds_and_refusal_is_not_retried(hub):
    c = Client(reply(text="not json at all"), reply(GOOD))
    assert call(LLM(client=c, guard=PIIGuard([]))) == GOOD and len(c.sent) == 2
    c2 = Client(reply(text="", stop="refusal"))
    with pytest.raises(SchemaError, match="declined"):
        call(LLM(client=c2, guard=PIIGuard([])))
    assert len(c2.sent) == 1


# ---------------------------------------------------------------- local response cache
def test_identical_request_is_answered_locally_without_api_call(conn):
    c = Client(reply(GOOD))
    l = LLM(client=c, conn=conn, guard=PIIGuard([]))
    assert call(l) == GOOD and call(l) == GOOD
    assert len(c.sent) == 1
    rows = conn.execute("SELECT local_cache_hit, cost_usd, input_tokens FROM llm_usage ORDER BY id").fetchall()
    assert [r["local_cache_hit"] for r in rows] == [0, 1] and rows[1]["cost_usd"] == 0 and rows[1]["input_tokens"] == 0


def test_any_change_to_the_request_misses_the_cache(conn):
    c = Client(reply(GOOD))
    l = LLM(client=c, conn=conn, guard=PIIGuard([]))
    call(l)
    call(l, user="a different posting")
    call(l, instructions="Do the thing differently")
    call(l, context="OTHER FACTS " * 50)
    assert len(c.sent) == 4


def test_cache_can_be_bypassed_per_call_and_per_client(conn):
    c = Client(reply(GOOD))
    l = LLM(client=c, conn=conn, guard=PIIGuard([]))
    call(l)
    call(l, use_cache=False)
    assert len(c.sent) == 2
    off = LLM(client=Client(reply(GOOD)), conn=conn, guard=PIIGuard([]), use_cache=False)
    call(off)
    call(off)
    assert len(off.client.sent) == 2


def test_failed_responses_are_never_cached(conn):
    c = Client(reply(text="garbage"))
    l = LLM(client=c, conn=conn, guard=PIIGuard([]))
    with pytest.raises(SchemaError):
        call(l)
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_pii_guard_runs_before_the_cache_lookup(conn):
    c = Client(reply(GOOD))
    l = LLM(client=c, conn=conn, guard=PIIGuard([]))
    call(l, user="my number is secret-token-12345")                          # cached while unguarded
    l.guard = PIIGuard(["secret-token-12345"])                               # guard now knows the value
    with pytest.raises(PIIGuardError):
        call(l, user="my number is secret-token-12345")                      # same request: must NOT be served from cache
    assert len(c.sent) == 1


def test_cache_stores_results_only_not_request_bodies(conn):
    l = LLM(client=Client(reply(GOOD)), conn=conn, guard=PIIGuard([]))
    call(l, user="CONFIDENTIAL POSTING BODY")
    dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM llm_cache")])
    assert "CONFIDENTIAL" not in dump and "FACTS" not in dump


def test_corrupt_cache_entry_is_discarded_and_refetched(conn):
    c = Client(reply(GOOD))
    l = LLM(client=c, conn=conn, guard=PIIGuard([]))
    call(l)
    conn.execute("UPDATE llm_cache SET result_json='{broken'")
    conn.commit()
    assert call(l) == GOOD and len(c.sent) == 2


def test_prune_removes_only_old_entries(conn):
    conn.execute("INSERT INTO llm_cache (key, stage, model, result_json, created_at) VALUES ('old','s','m','{}','2020-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO llm_cache (key, stage, model, result_json, created_at) VALUES ('new','s','m','{}',?)", (db.now(),))
    conn.commit()
    assert llmmod.prune_cache(conn, 60) == 1
    assert [r["key"] for r in conn.execute("SELECT key FROM llm_cache")] == ["new"]


# ---------------------------------------------------------------- cost and usage accounting
def test_cost_estimate_uses_current_prices_and_cache_multipliers():
    m = "claude-sonnet-5-5"
    assert config.PRICES[m] == {"in": 2.0, "out": 10.0}
    assert llmmod.estimate_cost(m, 1000, 100, 10000, 5000) == pytest.approx((2000 + 1000 + 2000 + 12500) / 1e6)
    assert llmmod.estimate_cost(m, 1000, 100, 10000, 5000, write_1h=5000) == pytest.approx((2000 + 1000 + 2000 + 20000) / 1e6)


def test_usage_rows_record_cache_tokens_and_ttl_breakdown(conn):
    l = LLM(client=Client(reply(GOOD, cr=4000, cw=0, inp=200, out=50)), conn=conn, guard=PIIGuard([]), use_cache=False)
    call(l)
    r = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert (r["cache_read_tokens"], r["cache_write_tokens"], r["input_tokens"]) == (4000, 0, 200)
    l2 = LLM(client=Client(reply(GOOD, cw=3000, w1h=3000)), conn=conn, guard=PIIGuard([]), use_cache=False)
    call(l2)
    cost = conn.execute("SELECT cost_usd FROM llm_usage ORDER BY id DESC").fetchone()[0]
    assert cost == pytest.approx(llmmod.estimate_cost("claude-sonnet-5-5", 100, 20, 0, 3000, 3000))


def seed(conn, stage, model, n, *, cr, cw, local=0):
    for i in range(n):
        conn.execute("INSERT INTO llm_usage (at, stage, model, request_id, input_tokens, output_tokens, cache_read_tokens,"
                     " cache_write_tokens, cost_usd, local_cache_hit) VALUES (?,?,?,?,?,?,?,?,?,0)",
                     (db.now(), stage, model, "r", 100, 50, cr if i else 0, cw if i == 0 else 0, 0.01))
    for _ in range(local):
        conn.execute("INSERT INTO llm_usage (at, stage, model, request_id, cost_usd, local_cache_hit) VALUES (?,?,?,?,0,1)",
                     (db.now(), stage, model, "local-cache"))
    conn.commit()


def test_usage_summary_savings_and_hints(conn):
    seed(conn, "evaluate", "claude-sonnet-5-5", 5, cr=4000, cw=4000, local=2)
    seed(conn, "eligibility", "claude-haiku-4-5-20251001", 4, cr=0, cw=0)       # prefix too short to cache on haiku
    s = usage.summary(conn, 30)
    ev = next(r for r in s["rows"] if r["stage"] == "evaluate")
    # 4 reads x 4000 tokens vs (500 uncached input + 4000 written) -> 16000 / 20500
    assert ev["api_calls"] == 5 and ev["local_hits"] == 2 and ev["cache_read_ratio"] == pytest.approx(16000 / 20500)
    assert ev["saved_prompt_cache"] > 0 and ev["saved_local"] == pytest.approx(0.05 / 5 * 2)
    assert not ev["hint"]
    el = next(r for r in s["rows"] if r["stage"] == "eligibility")
    assert "4096-token minimum" in el["hint"]
    text = usage.format_report(s)
    assert "evaluate" in text and "Local response cache: 2" in text


def test_usage_hint_when_cache_is_written_but_never_read(conn):
    seed(conn, "tailor", "claude-sonnet-5-5", 4, cr=0, cw=3000)
    for _ in range(3):   # make every call a writer, none a reader
        conn.execute("UPDATE llm_usage SET cache_write_tokens=3000, cache_read_tokens=0")
    conn.commit()
    assert "never read" in usage.summary(conn, 30)["rows"][0]["hint"]


def test_digest_and_health_include_caching_numbers(conn):
    seed(conn, "evaluate", "claude-sonnet-5-5", 3, cr=4000, cw=4000)
    assert "Caching (30 days)" in digest.build(conn)
    from src import actions
    assert actions.health(conn)["usage"]["totals"]["cache_read_ratio"] > 0


# ---------------------------------------------------------------- HTTP cache
class FakeHTTP:
    def __init__(self, body=b'{"jobs": []}', etag='"v1"', status=200):
        self.calls, self.body, self.etag, self.status = [], body, etag, status

    def get(self, url, **kw):
        self.calls.append(kw.get("headers", {}))
        status = self.status
        headers = {"etag": self.etag} if self.etag else {}
        return SimpleNamespace(status_code=status, headers=headers, content=b"" if status == 304 else self.body,
                               raise_for_status=lambda: None, json=lambda: json.loads(self.body))


def test_http_cache_fresh_then_revalidated_with_etag(conn, monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(ingest.httpx, "get", fake.get)
    ingest.set_http_cache(conn)
    try:
        assert ingest._get("https://x.test/api", params={"page": 1}).json() == {"jobs": []}
        r = ingest._get("https://x.test/api", params={"page": 1})
        assert r.from_cache == "fresh" and len(fake.calls) == 1                  # no second request
        assert ingest._get("https://x.test/api", params={"page": 2}).json() == {"jobs": []}
        assert len(fake.calls) == 2                                              # params are part of the key
        conn.execute("UPDATE http_cache SET fetched_at=?", (time.time() - 3600,))
        conn.commit()
        fake.status = 304
        r = ingest._get("https://x.test/api", params={"page": 1})
        assert r.from_cache == "revalidated" and r.json() == {"jobs": []}
        assert fake.calls[-1]["If-None-Match"] == '"v1"'
    finally:
        ingest.set_http_cache(None)


def test_http_cache_refetches_when_changed_and_is_off_by_default(conn, monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(ingest.httpx, "get", fake.get)
    ingest._get("https://x.test/a")
    ingest._get("https://x.test/a")
    assert len(fake.calls) == 2                                                  # cache disabled -> always fetches
    ingest.set_http_cache(conn)
    try:
        ingest._get("https://x.test/b")
        conn.execute("UPDATE http_cache SET fetched_at=?", (time.time() - 3600,))
        conn.commit()
        fake.body, fake.etag = b'{"jobs": [1]}', '"v2"'
        assert ingest._get("https://x.test/b").json() == {"jobs": [1]}           # 200 with new body replaces the entry
        assert ingest._get("https://x.test/b").from_cache == "fresh"
    finally:
        ingest.set_http_cache(None)


def test_run_all_enables_and_always_disables_the_http_cache(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(ingest, "PRIORITY_FETCHERS", {"infopark": lambda wl, known: seen.append(ingest._HTTP_CACHE["conn"]) or []})
    monkeypatch.setattr(ingest, "load_watchlist", lambda: [])
    monkeypatch.setattr(ingest, "fetch_remotive", lambda wl: [])
    monkeypatch.setattr(ingest, "fetch_himalayas", lambda wl: [])
    ingest.run_all(conn)
    assert seen == [conn] and ingest._HTTP_CACHE["conn"] is None


# ---------------------------------------------------------------- migration
def test_existing_v1_database_migrates_to_v2(hub):
    path = hub.path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA)
    raw.execute("PRAGMA user_version=1")
    raw.execute("INSERT INTO llm_usage (at, stage, model, cost_usd) VALUES ('t','s','m',1.5)")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert c.execute("SELECT cost_usd, local_cache_hit FROM llm_usage").fetchone()[:] == (1.5, 0)   # data kept
    c.execute("SELECT * FROM llm_cache")
    c.execute("SELECT * FROM http_cache")
    c.close()
