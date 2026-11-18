"""API endpoint tests with FastAPI TestClient."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "api.db"))
    db.init_db()
    return db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "")
    return TestClient(app)


def _audience(client):
    return client.post("/api/audiences", json={"name": "Main"}).json()


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_audience_crud(client):
    audience = _audience(client)
    assert audience["name"] == "Main"
    assert client.get("/api/audiences").json()[0]["contact_count"] == 0
    assert client.delete(f"/api/audiences/{audience['id']}").status_code == 204


def test_add_and_list_contacts_with_tags(client):
    audience = _audience(client)
    resp = client.post("/api/contacts", json={
        "email": "Alice@Example.com ", "audience_id": audience["id"],
        "first_name": "Alice", "tags": ["vip"],
    })
    assert resp.status_code == 201
    contact = resp.json()
    assert contact["email"] == "alice@example.com"
    assert contact["tags"] == ["vip"]

    data = client.get("/api/contacts?search=alice").json()
    assert data["total"] == 1
    assert data["contacts"][0]["status"] == "subscribed"

    readd = client.post("/api/contacts", json={
        "email": "alice@example.com", "audience_id": audience["id"],
    })
    assert readd.status_code == 201
    assert client.get("/api/contacts").json()["total"] == 1


def test_update_and_unsubscribe(client):
    audience = _audience(client)
    contact = client.post("/api/contacts", json={
        "email": "bob@example.com", "audience_id": audience["id"],
    }).json()

    updated = client.patch(f"/api/contacts/{contact['id']}", json={
        "status": "unsubscribed", "first_name": "Bob",
    }).json()
    assert updated["status"] == "unsubscribed"
    assert updated["first_name"] == "Bob"

    data = client.get("/api/contacts", params={"status": "subscribed"}).json()
    assert data["total"] == 0

    assert client.post("/api/unsubscribe", json={"email": "bob@example.com"}).status_code == 200
    assert client.post("/api/unsubscribe", json={"email": "nobody@example.com"}).status_code == 404


def test_campaign_flow_and_stats(client):
    audience = _audience(client)
    for i in range(3):
        client.post("/api/contacts", json={
            "email": f"user{i}@example.com", "audience_id": audience["id"],
        })
    campaign = client.post("/api/campaigns", json={
        "audience_id": audience["id"], "name": "March", "subject": "Hi",
        "html_content": "<p>hi</p>",
    }).json()
    assert campaign["status"] == "draft"
    assert campaign["audience_id"] == audience["id"]

    detail = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert detail["recipient_count"] == 3

    blocked = client.post(f"/api/campaigns/{campaign['id']}/send").json()
    assert blocked["status"] == "blocked"
    assert blocked["recipients"] == 3
    assert blocked["smtp_configured"] is False

    dry = client.post(f"/api/campaigns/{campaign['id']}/send?dry_run=true").json()
    assert dry["status"] == "dry_run"

    assert client.patch(f"/api/campaigns/{campaign['id']}",
                        json={"subject": "Updated"}).json()["subject"] == "Updated"
    assert client.get(f"/api/campaigns/{campaign['id']}/stats").json()["sent"] == 0


def test_edit_sent_campaign_rejected(client, monkeypatch):
    import app.main as main

    audience = _audience(client)
    campaign = client.post("/api/campaigns", json={
        "audience_id": audience["id"], "name": "X", "subject": "s",
        "html_content": "<p>x</p>",
    }).json()

    monkeypatch.setattr(main.mailer, "is_configured", lambda: True)
    monkeypatch.setattr(main.mailer, "send_email", lambda *a, **kw: None)
    assert client.post(f"/api/campaigns/{campaign['id']}/send").json()["status"] == "sent"
    assert client.patch(f"/api/campaigns/{campaign['id']}",
                        json={"subject": "no"}).status_code == 400
    assert client.delete(f"/api/campaigns/{campaign['id']}").status_code == 400


def test_track_open_click(client):
    audience = _audience(client)
    contact = client.post("/api/contacts", json={
        "email": "user@example.com", "audience_id": audience["id"],
    }).json()
    campaign = client.post("/api/campaigns", json={
        "audience_id": audience["id"], "name": "X", "subject": "s",
        "html_content": "<p>x</p>",
    }).json()

    for event in ("open", "open", "click"):
        client.post(f"/api/campaigns/{campaign['id']}/track/{event}",
                    json={"email": "user@example.com"})
    stats = client.get(f"/api/campaigns/{campaign['id']}/stats").json()
    assert stats["opens"] == 2
    assert stats["clicks"] == 1
    assert client.post(f"/api/campaigns/{campaign['id']}/track/nope",
                       json={"email": "user@example.com"}).status_code == 400


def test_import_endpoint_validates_key(client):
    resp = client.post("/api/import/mailchimp", json={"api_key": "short"})
    assert resp.status_code == 422


def test_unsub_page_served(client):
    resp = client.get("/unsubscribe?email=a@b.c")
    assert resp.status_code == 200
    assert "Unsubscribe" in resp.text


def test_dashboard_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Newsletter Agent" in resp.text
