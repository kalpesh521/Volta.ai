"""RAG answers cite documents. Live questions do not read the knowledge store for SOC."""
from app.core.config import settings
from app.modules.assistant.deps import get_structured_llm
from tests.assistant_helpers import FakeStructuredLLM, auth_headers
from tests.test_knowledge_api import MANUAL, _file, _promote

ASK = "/assistant/me/ask"


async def test_fault_code_without_a_manual_does_not_guess(client, fake_llm):
    headers = await auth_headers(client, "no-manual@example.com")
    from tests.onboarding_helpers import complete_hybrid_onboarding

    await complete_hybrid_onboarding(client, headers)
    response = await client.post(ASK, json={"question": "What does fault code E04 mean?"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intents"][0] == "document"
    assert "search_knowledge" in {call["name"] for call in body["meta"]["tool_calls"]}
    assert body["sources"] == []
    assert body["meta"]["fallback_reason"] == "knowledge_gap"
    assert "installer" in body["answer"]["recommendation"].lower()
    assert fake_llm.calls == []


async def test_admin_manual_is_cited_as_soon_as_it_is_uploaded(client, db_session, fake_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    from tests.onboarding_helpers import complete_hybrid_onboarding

    admin = await auth_headers(client, "manual-admin@example.com")
    await _promote(db_session, "manual-admin@example.com")
    await complete_hybrid_onboarding(client, admin)

    uploaded = await client.post(
        "/knowledge/admin/documents",
        headers=admin,
        data={"doc_type": "inverter_manual", "title": "Growatt fault codes", "brand": "Growatt"},
        files=_file("growatt.txt", MANUAL),
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["status"] == "published"

    shown = await client.post(
        ASK, json={"question": "What does fault code E04 mean on my inverter?"}, headers=admin
    )
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert "search_knowledge" in body["tools_used"]
    assert body["sources"]
    assert body["sources"][0]["title"] == "Growatt fault codes"
    assert body["sources"][0]["page"] == 1
    assert "Sources:" in body["answer"]["explanation"]
    assert "E04" in fake_llm.last_prompt()
    assert "soc_percent" not in fake_llm.last_prompt() or "knowledge" in fake_llm.last_prompt()


async def test_private_bill_is_not_retrieved_for_another_user(client, db_session, fake_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    from tests.onboarding_helpers import complete_hybrid_onboarding

    owner = await auth_headers(client, "bill-rag-owner@example.com")
    other = await auth_headers(client, "bill-rag-other@example.com")
    await complete_hybrid_onboarding(client, owner)
    await complete_hybrid_onboarding(client, other, create_new=False)
    uploaded = await client.post(
        "/knowledge/documents",
        headers=owner,
        data={"doc_type": "electricity_bill", "title": "March bill"},
        files=_file(
            "bill.txt",
            "Units consumed: 320 kWh\nTariff category: LT-I Residential\nSanctioned load: 5 kW\n"
            "Amount payable: Rs 2450\nPrivate March bill for this account only.\n",
        ),
    )
    assert uploaded.status_code == 201, uploaded.text

    leaked = await client.post(ASK, json={"question": "Explain my electricity bill"}, headers=other)
    assert leaked.status_code == 200, leaked.text
    assert leaked.json()["sources"] == []
    assert "320" not in leaked.json()["answer"]["observation"]


import pytest
from main import app


@pytest.fixture
def fake_llm(client):
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake
