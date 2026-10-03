"""The guard must refuse any request containing contact.yaml / profile.yaml values (plan.md §3)."""
from types import SimpleNamespace

import pytest

from src.llm import LLM, PIIGuard, PIIGuardError

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class FakeClient:
    def __init__(self):
        self.sent = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.sent.append(req)
        block = SimpleNamespace(type="text", text='{"ok": true}')
        return SimpleNamespace(content=[block], id="req_1", stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


def call(llm, user="Hello", instructions="Be brief", context=""):
    return llm.call(stage="t", model="claude-haiku-4-5-20251001", instructions=instructions,
                    context=context, user=user, schema=SCHEMA)


@pytest.fixture
def llm(hub):
    client = FakeClient()
    return LLM(client=client), client


def test_clean_request_is_sent(llm):
    l, client = llm
    assert call(l, user="a normal job posting about Python") == {"ok": True}
    assert len(client.sent) == 1


@pytest.mark.parametrize("leak", [
    "jane.example@example.com", "JANE.EXAMPLE@EXAMPLE.COM",       # email, any case
    "+91 98765 43210", "9876543210", "91-98765-43210",            # phone in any format
    "2500000", "2,500,000",                                        # pay floor (whole number)
    "1800000", "Bengaluru, India", "janeexample",                  # CTC, address, profile url fragment
])
def test_pii_values_are_refused(llm, leak):
    l, client = llm
    for slot in ("user", "instructions", "context"):
        with pytest.raises(PIIGuardError):
            call(l, **{slot: f"posting text {leak} more text"})
    assert client.sent == []          # nothing ever reached the client


def test_short_generic_numbers_do_not_false_positive(llm):
    l, _ = llm
    call(l, user="Notice period of 30 days, up to 25 LPA, 2023-01 to present")


def test_guard_without_files_allows_everything(hub):
    assert PIIGuard([]).check("anything at all 9876543210") is None


def test_usage_is_recorded_without_bodies(hub, conn):
    client = FakeClient()
    l = LLM(client=client, conn=conn)
    call(l, user="secret-ish posting body")
    row = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert row["input_tokens"] == 10 and row["request_id"] == "req_1"
    assert "secret-ish" not in " ".join(str(v) for v in tuple(row))
