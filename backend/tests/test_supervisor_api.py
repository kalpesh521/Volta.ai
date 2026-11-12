"""
The v2 supervisor over HTTP: real onboarding, ingest, energy store, knowledge store, action workflow
and LangGraph; only the LLM is faked.

The first group proves parity with v1 on the same data, because v2 may only replace v1 once it answers
the same questions the same way. The rest covers what is new: several agents, memory, ownership of
conversations, device actions, and streaming.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from httpx import AsyncClient

from app.ai.config import LLMRole
from app.core.config import settings
from app.agents_platform.agents.device_action.deps import get_command_publisher
from app.agents_platform.agents.device_action.publisher import RecordingPublisher
from app.modules.assistant.deps import get_structured_llm
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding
from tests.test_assistant_api import _ask as ask_v1
from tests.test_assistant_api import _home
from tests.test_knowledge_api import MANUAL, _file, _promote
from tests.test_safe_actions import HEATER

V2 = "/v2/assistant/me/ask"
STREAM = "/v2/assistant/me/ask/stream"
BATTERY_Q = "Why is the battery discharging?"


@pytest.fixture
def fake_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake


@pytest.fixture
def publisher(monkeypatch) -> RecordingPublisher:
    recorder = RecordingPublisher()
    app.dependency_overrides[get_command_publisher] = lambda: recorder
    monkeypatch.setattr(settings, "DEVICE_CONTROL_ENABLED", True)
    monkeypatch.setattr(settings, "DEVICE_KILL_SWITCH", False)
    return recorder


async def ask_v2(
    client: AsyncClient, headers: dict[str, str], question: str, conversation_id: str | None = None
) -> dict:
    body: dict = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.post(V2, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def assert_same_freshness(left: dict, right: dict) -> None:
    """Two requests run moments apart, so the age differs by a second or so; everything else must match."""
    assert {k: v for k, v in left.items() if k != "age_seconds"} == {
        k: v for k, v in right.items() if k != "age_seconds"
    }
    assert abs((left["age_seconds"] or 0) - (right["age_seconds"] or 0)) <= 5


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in filter(None, text.split("\n\n")):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        events.append((name, data))
    return events


# --- parity with v1 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        BATTERY_Q,
        "How much solar did I generate today?",
        "Should I run the washing machine now?",
        "How long can the battery power my home?",
        "Give me a full report on my home",
    ],
)
async def test_v2_answers_a_home_question_the_way_v1_does(client, fake_llm, question):
    headers, _, _ = await _home(client, f"parity-{abs(hash(question))}@example.com")

    v1 = await ask_v1(client, headers, question)
    v2 = await ask_v2(client, headers, question)

    assert v2["intents"] == v1["intents"]
    assert sorted(v2["tools_used"]) == sorted(v1["tools_used"])
    assert v2["analytics_used"] == v1["analytics_used"]
    assert v2["answer"] == v1["answer"]
    assert v2["confidence"] == v1["confidence"]
    assert_same_freshness(v2["data_freshness"], v1["data_freshness"])
    assert v2["meta"]["llm_used"] is True and v1["meta"]["llm_used"] is True
    assert v2["route"] == "single" and [a["agent"] for a in v2["agents"]] == ["energy_insight"]
    assert v2["meta"]["usage"]["total_tokens"] == v1["meta"]["usage"]["total_tokens"]


@pytest.mark.parametrize(
    "question",
    [
        BATTERY_Q,
        "How much solar did I generate today?",
        "Should I run the washing machine now?",
        "How long can the battery power my home?",
        "Give me a full report on my home",
    ],
)
async def test_v2_sends_the_model_exactly_the_prompt_v1_sends(client, fake_llm, question):
    """
    The strongest parity check there is, and free. Same data, same question: if the prompt is identical then any
    difference between two real-model answers is sampling, not a difference between v1 and v2. A real-model run once
    showed different wording across the endpoints; this test is what showed it was not v2.
    """
    headers, _, _ = await _home(client, f"prompt-parity-{abs(hash(question))}@example.com")

    def sent() -> list:
        # The two requests run a moment apart, so the data's age differs by a second or so. Nothing else may.
        return [
            (role, [(m.type, re.sub(r'"age_seconds":\d+', '"age_seconds":N', str(m.content))) for m in messages])
            for role, _, messages in fake_llm.calls
        ]

    await ask_v1(client, headers, question)
    v1 = sent()
    fake_llm.calls.clear()
    await ask_v2(client, headers, question)
    v2 = sent()

    assert v1 and v1 == v2


async def test_v2_matches_v1_when_there_is_no_telemetry(client, fake_llm):
    headers, _, _ = await _home(client, "parity-nodata@example.com", ts=None)

    v1 = await ask_v1(client, headers, BATTERY_Q)
    v2 = await ask_v2(client, headers, BATTERY_Q)

    assert v2["answer"] == v1["answer"]
    assert v2["meta"]["fallback_reason"] == v1["meta"]["fallback_reason"] == "no_data"
    assert v2["confidence"] == v1["confidence"]
    assert fake_llm.calls == []


async def test_v2_matches_v1_on_a_document_gap_and_never_asks_the_model_to_guess(client, fake_llm):
    headers, _, _ = await _home(client, "parity-gap@example.com")
    question = "What does fault code E04 mean?"

    v1 = await ask_v1(client, headers, question)
    v2 = await ask_v2(client, headers, question)

    assert v2["answer"] == v1["answer"]
    assert v2["meta"]["fallback_reason"] == "knowledge_gap"
    assert v2["sources"] == [] and v2["route"] == "single"
    assert [a["agent"] for a in v2["agents"]] == ["knowledge"]
    assert fake_llm.calls == []


async def test_an_off_topic_question_is_refused_without_calling_any_agent(client, fake_llm):
    headers, _, _ = await _home(client, "v2-offtopic@example.com")
    body = await ask_v2(client, headers, "What is the capital of France?")

    assert body["route"] == "refusal" and body["agents"] == []
    assert body["meta"]["fallback_reason"] == "out_of_scope"
    assert fake_llm.roles_called() == [LLMRole.ROUTER]  # only the router, never the answer model


# --- several agents -------------------------------------------------------------------------------


async def test_a_manual_and_live_data_question_runs_two_agents_and_one_answer_call(
    client, db_session, fake_llm, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    email = "v2-compound@example.com"
    headers, _, _ = await _home(client, email)
    await _promote(db_session, email)
    uploaded = await client.post(
        "/knowledge/admin/documents",
        headers=headers,
        data={"doc_type": "inverter_manual", "title": "Growatt fault codes", "brand": "Growatt"},
        files=_file("growatt.txt", MANUAL),
    )
    assert uploaded.status_code == 201, uploaded.text

    body = await ask_v2(client, headers, "What does fault code E04 mean and how is my battery doing?")

    assert body["route"] == "multi"
    assert {a["agent"] for a in body["agents"]} == {"energy_insight", "knowledge"}
    assert set(body["intents"]) == {"document", "battery"}
    assert body["sources"] and body["sources"][0]["title"] == "Growatt fault codes"
    assert "Sources:" in body["answer"]["explanation"]
    assert fake_llm.roles_called() == [LLMRole.ANSWER]
    prompt = fake_llm.last_prompt()
    assert "E04" in prompt and "soc_percent" in prompt
    assert {"search_knowledge", "get_battery_status"} <= set(body["tools_used"])


# --- conversation memory --------------------------------------------------------------------------


async def test_a_follow_up_in_the_same_conversation_inherits_the_topic(client, fake_llm):
    headers, _, _ = await _home(client, "v2-memory@example.com")

    first = await ask_v2(client, headers, BATTERY_Q)
    second = await ask_v2(client, headers, "why?", first["conversation_id"])

    assert second["conversation_id"] == first["conversation_id"]
    assert second["meta"]["classification_method"] == "memory"
    assert second["intents"] == first["intents"] == ["battery"]
    assert fake_llm.roles_called() == [LLMRole.ANSWER, LLMRole.ANSWER]  # no router call for the follow-up
    assert BATTERY_Q in fake_llm.last_prompt()


async def test_the_same_follow_up_without_a_conversation_is_not_guessed(client, fake_llm):
    headers, _, _ = await _home(client, "v2-nomemory@example.com")
    await ask_v2(client, headers, BATTERY_Q)
    second = await ask_v2(client, headers, "why?")  # no conversation_id: a fresh thread

    assert second["meta"]["classification_method"] != "memory"


async def test_a_new_conversation_gets_a_fresh_id_each_time(client, fake_llm):
    headers, _, _ = await _home(client, "v2-newconv@example.com")
    a = await ask_v2(client, headers, BATTERY_Q)
    b = await ask_v2(client, headers, BATTERY_Q)
    assert a["conversation_id"] != b["conversation_id"]


async def test_nobody_can_resume_another_users_conversation_or_probe_for_ids(client, fake_llm):
    owner, _, _ = await _home(client, "v2-conv-owner@example.com")
    other, _, _ = await _home(client, "v2-conv-other@example.com")
    conversation_id = (await ask_v2(client, owner, BATTERY_Q))["conversation_id"]

    foreign = await client.post(V2, json={"question": "why?", "conversation_id": conversation_id}, headers=other)
    unknown = await client.post(V2, json={"question": "why?", "conversation_id": "0" * 32}, headers=other)

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert foreign.json()["error"]["code"] == "conversation_not_found"


async def test_a_malformed_conversation_id_is_rejected_by_validation(client, fake_llm):
    headers, _, _ = await _home(client, "v2-badid@example.com")
    response = await client.post(V2, json={"question": BATTERY_Q, "conversation_id": "../etc"}, headers=headers)
    assert response.status_code == 400  # this API reports validation failures as 400
    assert response.json()["error"]["code"] == "validation_error"
    assert "conversation_id" in response.json()["error"]["message"]


async def test_the_endpoint_requires_authentication(client, fake_llm):
    assert (await client.post(V2, json={"question": BATTERY_Q})).status_code in (401, 403)


async def test_a_named_home_must_belong_to_the_caller(client, fake_llm):
    owner, household_id, _ = await _home(client, "v2-home-owner@example.com")
    other, _, _ = await _home(client, "v2-home-other@example.com")

    allowed = await client.post(f"/v2/assistant/{household_id}/ask", json={"question": BATTERY_Q}, headers=owner)
    denied = await client.post(f"/v2/assistant/{household_id}/ask", json={"question": BATTERY_Q}, headers=other)

    assert allowed.status_code == 200 and allowed.json()["household_id"] == household_id
    assert denied.status_code == 404


# --- device actions -------------------------------------------------------------------------------


async def _heater_home(client: AsyncClient, email: str) -> dict[str, str]:
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(client, headers)
    now = datetime.now(TZ).replace(microsecond=0)
    await ingest(
        client, make_tick(now, household_id=household_id, soc=70, solar_kw=3.2, load_kw=0.8, devices=[HEATER])
    )
    return headers


async def test_a_control_request_becomes_a_proposal_that_only_the_user_can_confirm(
    client, fake_llm, publisher
):
    headers = await _heater_home(client, "v2-action@example.com")

    body = await ask_v2(client, headers, "Run the water heater now.")

    assert body["route"] == "exclusive" and [a["agent"] for a in body["agents"]] == ["device_action"]
    proposal = body["proposal"]
    assert proposal["status"] == "awaiting_confirmation" and proposal["device"] == "water_heater"
    assert body["answer"]["observation"] == proposal["message"]
    assert "Nothing has changed" in body["answer"]["explanation"]
    assert publisher.commands == [], "asking must never send a command"
    assert fake_llm.calls == [], "no model is involved in a device action"

    confirmed = await client.post(
        f"/assistant/actions/{proposal['proposal_id']}/confirm", json={"confirmed": True}, headers=headers
    )
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "queued"
    assert len(publisher.commands) == 1


async def test_a_control_request_is_refused_while_device_control_is_off(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_CONTROL_ENABLED", False)
    recorder = RecordingPublisher()
    app.dependency_overrides[get_command_publisher] = lambda: recorder
    headers = await _heater_home(client, "v2-action-off@example.com")

    body = await ask_v2(client, headers, "Run the water heater now.")

    assert body["proposal"]["status"] == "rejected"
    assert body["agents"][0]["status"] == "refused"
    assert "switched off" in body["answer"]["observation"]
    assert recorder.commands == []


# --- streaming ------------------------------------------------------------------------------------


async def test_the_stream_sends_progress_then_one_final_answer(client, fake_llm, db_scope):
    headers, _, _ = await _home(client, "v2-stream@example.com")

    response = await client.post(STREAM, json={"question": BATTERY_Q}, headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[-1] == "final" and names.count("final") == 1
    assert set(names[:-1]) == {"status"}
    stages = [data["stage"] for name, data in events if name == "status"]
    assert stages[0] == "understanding" and "agent_started" in stages and stages[-1] == "synthesizing"

    final = events[-1][1]
    assert final["answer"]["observation"] == "LLM observation."
    assert final["conversation_id"] and final["route"] == "single"


async def test_a_streamed_answer_equals_the_non_streamed_one(client, fake_llm, db_scope):
    headers, _, _ = await _home(client, "v2-stream-equal@example.com")
    plain = await ask_v2(client, headers, BATTERY_Q)

    streamed = parse_sse((await client.post(STREAM, json={"question": BATTERY_Q}, headers=headers)).text)[-1][1]

    for key in ("answer", "intents", "confidence", "tools_used"):
        assert streamed[key] == plain[key], key
    assert_same_freshness(streamed["data_freshness"], plain["data_freshness"])


async def test_a_stream_continues_a_conversation(client, fake_llm, db_scope):
    headers, _, _ = await _home(client, "v2-stream-memory@example.com")
    first = await ask_v2(client, headers, BATTERY_Q)

    events = parse_sse(
        (
            await client.post(
                STREAM, json={"question": "why?", "conversation_id": first["conversation_id"]}, headers=headers
            )
        ).text
    )

    assert events[-1][1]["meta"]["classification_method"] == "memory"


async def test_a_stream_for_an_unknown_conversation_is_a_plain_404_not_a_broken_stream(
    client, fake_llm, db_scope
):
    headers, _, _ = await _home(client, "v2-stream-404@example.com")

    response = await client.post(STREAM, json={"question": "why?", "conversation_id": "1" * 32}, headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"
