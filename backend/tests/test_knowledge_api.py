"""Document upload for a logged-in user and for a Suryaa admin."""
from sqlalchemy import func, select

from app.core.config import settings
from app.models.user import User
from app.modules.knowledge.models import KnowledgeChunk
from tests.assistant_helpers import auth_headers

BILL = (
    "Units consumed: 320 kWh\n"
    "Tariff category: LT-I Residential\n"
    "Sanctioned load: 5 kW\n"
    "Amount payable: Rs 2450\n"
    "This March bill is private to the account holder.\n"
)
MANUAL = (
    "Growatt inverter fault code E04 means the PV voltage is too high. "
    "Turn the DC switch off and contact the installer. Do not guess another meaning.\n"
)


async def _promote(db_session, email: str) -> None:
    user = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
    user.is_admin = True
    await db_session.commit()


def _file(name: str, text: str) -> dict:
    return {"file": (name, text.encode(), "text/plain")}


async def test_user_uploads_a_private_bill_and_another_user_cannot_see_it(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    owner = await auth_headers(client, "bill-owner@example.com")
    other = await auth_headers(client, "bill-other@example.com")

    created = await client.post(
        "/knowledge/documents",
        headers=owner,
        data={"doc_type": "electricity_bill", "title": "March bill"},
        files=_file("march-bill.txt", BILL),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scope"] == "private"
    assert body["status"] == "published"
    assert body["bill"]["units_kwh"] == 320
    assert body["bill"]["tariff_category"].startswith("LT-I")
    assert body["bill"]["sanctioned_load_kw"] == 5
    assert body["chunk_count"] >= 1

    listing = await client.get("/knowledge/documents", headers=other)
    assert listing.status_code == 200
    assert listing.json() == []

    hidden = await client.get(f"/knowledge/documents/{body['id']}", headers=other)
    assert hidden.status_code == 404

    removed = await client.delete(f"/knowledge/documents/{body['id']}", headers=owner)
    assert removed.status_code == 204
    chunks = await db_session.scalar(select(func.count()).select_from(KnowledgeChunk))
    assert chunks == 0


async def test_blank_optional_fields_are_ignored(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    headers = await auth_headers(client, "blank-fields-admin@example.com")
    await _promote(db_session, "blank-fields-admin@example.com")
    response = await client.post(
        "/knowledge/admin/documents",
        headers=headers,
        data={
            "doc_type": "inverter_manual",
            "title": "Huawei User Manual",
            "brand": "Huawei",
            "discom": "",
            "effective_date": "",
            "replaces_document_id": "",
        },
        files=_file("huawei.txt", MANUAL),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "published"


async def test_user_cannot_upload_a_shared_manual(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    headers = await auth_headers(client, "not-admin@example.com")
    response = await client.post(
        "/knowledge/documents",
        headers=headers,
        data={"doc_type": "inverter_manual", "title": "Growatt"},
        files=_file("manual.txt", MANUAL),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_admin_upload_publishes_immediately_and_rollback_restores(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    headers = await auth_headers(client, "suryaa-admin@example.com")
    await _promote(db_session, "suryaa-admin@example.com")

    denied = await auth_headers(client, "member@example.com")
    forbidden = await client.post(
        "/knowledge/admin/documents",
        headers=denied,
        data={"doc_type": "inverter_manual", "title": "Nope", "brand": "Growatt"},
        files=_file("nope.txt", MANUAL),
    )
    assert forbidden.status_code == 403

    first = await client.post(
        "/knowledge/admin/documents",
        headers=headers,
        data={"doc_type": "inverter_manual", "title": "Growatt manual v1", "brand": "Growatt"},
        files=_file("v1.txt", MANUAL),
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]
    assert first.json()["status"] == "published"

    second = await client.post(
        "/knowledge/admin/documents",
        headers=headers,
        data={
            "doc_type": "inverter_manual",
            "title": "Growatt manual v2",
            "brand": "Growatt",
            "replaces_document_id": first_id,
        },
        files=_file("v2.txt", MANUAL + "\nVersion two adds a note about the DC switch.\n"),
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    assert second.json()["version_number"] == 2
    assert second.json()["status"] == "published"

    detail = await client.get(f"/knowledge/admin/documents/{first_id}", headers=headers)
    assert detail.json()["status"] == "superseded"
    assert len(detail.json()["versions"]) == 2

    restored = await client.post(
        f"/knowledge/admin/documents/{first_id}/rollback",
        headers=headers,
        params={"note": "v2 was wrong"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "published"
    current = await client.get(f"/knowledge/admin/documents/{second_id}", headers=headers)
    assert current.json()["status"] == "superseded"
