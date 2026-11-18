"""Tests for the expanded platform: automations, segments, A/B, commerce,
publishing, growth and analytics."""
import time

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "platform.db"))
    db.init_db()
    monkeypatch.setenv("SMTP_HOST", "")
    return db


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def audience(client):
    return client.post("/api/audiences", json={"name": "Audience"}).json()


@pytest.fixture
def contacts(client, audience):
    out = []
    for email, tag in (("a@example.com", "vip"), ("b@example.com", None), ("c@example.com", "vip")):
        c = client.post("/api/contacts", json={
            "email": email, "audience_id": audience["id"],
            "tags": [tag] if tag else [],
        }).json()
        out.append(c)
    return out


# ---------- templates + sequences / automations --------------------------------------

def test_template_crud(client):
    t = client.post("/api/templates", json={"name": "Master", "subject": "Hi",
                                            "html_content": "<p>hi</p>"}).json()
    assert t["name"] == "Master"
    assert len(client.get("/api/templates").json()) == 1
    assert client.delete(f"/api/templates/{t['id']}").status_code == 204


def test_welcome_sequence_flow(client, audience, contacts):
    seq = client.post("/api/automations/sequences", json={
        "name": "Welcome", "audience_id": audience["id"], "trigger_type": "signup",
    }).json()
    client.post(f"/api/automations/sequences/{seq['id']}/steps", json={
        "position": 0, "subject": "Welcome!", "html_content": "<p>Hey</p>", "delay_hours": 0 })
    client.post(f"/api/automations/sequences/{seq['id']}/steps", json={
        "position": 1, "subject": "Day 2", "html_content": "<p>More</p>", "delay_hours": 48 })
    client.patch(f"/api/automations/sequences/{seq['id']}", json={"status": "active"})

    new_contact = client.post("/api/contacts", json={
        "email": "new@example.com", "audience_id": audience["id"] }).json()
    enrollments = client.get("/api/automations/enrollments?status=active").json()
    assert len(enrollments) == 1
    assert enrollments[0]["email"] == "new@example.com"

    result = client.post("/api/automations/tick").json()
    assert result["sends"] == 1  # SMTP off: recorded as sent

    enrollments = client.get("/api/automations/enrollments?status=active").json()
    assert enrollments[0]["next_step"] == 1
    assert enrollments[0]["next_send_at"] > time.time()  # 48h delay scheduled


def test_enroll_by_tag_and_segment(client, audience, contacts):
    seq = client.post("/api/automations/sequences", json={"name": "VIP drip"}).json()
    client.patch(f"/api/automations/sequences/{seq['id']}", json={"status": "active"})
    r = client.post(f"/api/automations/sequences/{seq['id']}/enroll",
                    json={"tag": "vip"}).json()
    assert r["enrolled"] == 2
    again = client.post(f"/api/automations/sequences/{seq['id']}/enroll",
                        json={"tag": "vip"}).json()
    assert again["enrolled"] == 0  # idempotent


def test_unsubscribed_stops_sequence(client, audience, contacts):
    seq = client.post("/api/automations/sequences", json={"name": "Drip"}).json()
    client.post(f"/api/automations/sequences/{seq['id']}/steps", json={
        "position": 0, "subject": "s", "html_content": "h" })
    client.patch(f"/api/automations/sequences/{seq['id']}", json={"status": "active"})
    client.post(f"/api/automations/sequences/{seq['id']}/enroll",
                json={"contact_id": contacts[0]["id"]})
    client.patch(f"/api/contacts/{contacts[0]['id']}", json={"status": "unsubscribed"})
    result = client.post("/api/automations/tick").json()
    assert result["sends"] == 0


# ---------- segments -------------------------------------------------------------------

def test_segment_tag_and_event_rules(client, audience, contacts):
    client.post("/api/products", json={"name": "Ebook", "price_cents": 990, "published": True})
    products = client.get("/api/products").json()
    client.post("/api/orders", json={"product_id": products[0]["id"],
                                     "email": "a@example.com"})
    segment = client.post("/api/segments", json={
        "name": "VIP buyers",
        "rules": [{"field": "tag", "op": "any", "value": "vip"},
                  {"field": "event", "op": "has", "value": "purchase"}],
    }).json()
    preview = client.post(f"/api/segments/{segment['id']}/preview").json()
    assert preview["total"] == 1
    assert preview["contacts"][0]["email"] == "a@example.com"


def test_segment_status_rule(client, audience, contacts):
    segment = client.post("/api/segments", json={
        "name": "All subscribed",
        "rules": [{"field": "status", "op": "eq", "value": "subscribed"}],
    }).json()
    preview = client.post(f"/api/segments/{segment['id']}/preview").json()
    assert preview["total"] == 3


# ---------- A/B testing -----------------------------------------------------------------

def test_email_ab_test_split_and_winner(client, audience, contacts, monkeypatch):
    monkeypatch.setattr("app.mailer.is_configured", lambda: True)
    monkeypatch.setattr("app.mailer.send_email", lambda *a, **k: None)
    campaign = client.post("/api/campaigns", json={
        "audience_id": audience["id"], "name": "Test", "subject": "A subject",
        "html_content": "<p>x</p>" }).json()
    test = client.post(f"/api/campaigns/{campaign['id']}/ab-tests", json={
        "name": "Subject", "variants": [
            {"label": "A", "subject": "Subject A"},
            {"label": "B", "subject": "Subject B"}] }).json()
    assert test["status"] == "running"

    send = client.post(f"/api/campaigns/{campaign['id']}/send").json()
    assert send["status"] == "sent"
    assert send["ab_groups"] == {"A": 1, "B": 2}  # deterministic md5 split

    # each variant must have received recipients
    report = client.get(f"/api/ab-tests/{test['id']}/report").json()
    counts = {v["label"]: v["recipients"] for v in report["variants"]}
    assert counts == {"A": 1, "B": 2}

    # one open for B (1/2 = 50%) vs one for A (1/1 = 100%) → A wins
    client.post(f"/api/campaigns/{campaign['id']}/track/open",
                json={"email": "b@example.com"})
    client.post(f"/api/campaigns/{campaign['id']}/track/open",
                json={"email": "a@example.com"})
    winner = client.post(f"/api/ab-tests/{test['id']}/winner").json()
    assert winner["winner"] == "A"
    report = client.get(f"/api/ab-tests/{test['id']}/report").json()
    assert report["test"]["status"] == "done"


def test_ab_test_requires_two_variants(client, audience, contacts):
    campaign = client.post("/api/campaigns", json={
        "audience_id": audience["id"], "name": "T", "subject": "s",
        "html_content": "x" }).json()
    resp = client.post(f"/api/campaigns/{campaign['id']}/ab-tests", json={
        "name": "bad", "variants": [{"label": "A", "subject": "s"}] })
    assert resp.status_code == 400


def test_landing_page_ab_variants(client, audience, contacts):
    page = client.post("/api/pages", json={
        "title": "Launch", "slug": "launch", "audience_id": audience["id"],
        "html_content": "<h1>Version A</h1>", "published": True }).json()
    client.post(f"/api/pages/{page['id']}/variants", json={
        "label": "B", "html_content": "<h1>Version B</h1>" })
    anonymous = client.get("/p/launch")
    assert anonymous.status_code == 200
    sub = client.post("/p/launch/subscribe", data={"email": "visitor@example.com"})
    assert sub.status_code == 200
    report = client.post(f"/api/pages/{page['id']}/ab-report").json()
    total_views = sum(r["views"] for r in report["report"])
    total_signups = sum(r["conversions"] for r in report["report"])
    assert total_views >= 1 and total_signups >= 1


# ---------- commerce ----------------------------------------------------------------------

def test_product_purchase_and_analytics(client, audience, contacts):
    product = client.post("/api/products", json={
        "name": "Course", "price_cents": 4900, "product_type": "course", "published": True }).json()
    assert client.get(f"/shop/{product['slug']}").status_code == 200
    page = client.get(f"/shop/{product['slug']}")
    assert "Course" in page.text

    order = client.post("/api/orders", json={"product_id": product["id"],
                                             "email": "a@example.com"}).json()
    assert order["amount_cents"] == 4900
    assert order["status"] == "paid"
    orders = client.get("/api/orders").json()
    assert orders[0]["product_name"] == "Course"
    event = client.get("/api/analytics/overview").json()
    assert event["revenue"]["product_revenue_cents"] == 4900


def test_subscription_lifecycle(client):
    plan = client.post("/api/subscription-plans", json={
        "name": "Premium", "price_cents": 1000, "published": True }).json()
    sub = client.post("/api/subscriptions", json={
        "plan_id": plan["id"], "email": "a@example.com" }).json()
    assert sub["status"] == "active"
    subs = client.get("/api/subscriptions").json()
    assert subs[0]["plan_name"] == "Premium"
    creator = client.get("/api/analytics/creator").json()
    assert creator["active_paid_subscriptions"] == 1
    assert creator["estimated_mrr_cents"] == 1000
    client.post(f"/api/subscriptions/{sub['id']}/cancel")
    assert client.get("/api/subscriptions").json()[0]["status"] == "canceled"


def test_booking_flow(client):
    service = client.post("/api/services", json={
        "name": "Coaching", "duration_min": 30, "price_cents": 5000,
        "availability": '{"Mon": ["09:00-10:00"], "Tue": ["09:00-10:00"]}',
        "published": True }).json()
    slots = client.get(f"/api/services/{service['id']}/slots").json()
    assert slots["slots"], "expected generated slots"
    booking = client.post(f"/api/services/{service['id']}/book", json={
        "email": "a@example.com", "slot_ts": slots["slots"][0]["ts"] }).json()
    assert booking["status"] == "booked"
    # same slot can't be double-booked
    conflict = client.post(f"/api/services/{service['id']}/book", json={
        "email": "b@example.com", "slot_ts": slots["slots"][0]["ts"] })
    assert conflict.status_code == 409
    assert len(client.get("/api/bookings").json()) == 1


# ---------- publishing / pages / forms -------------------------------------------------------

def test_post_lifecycle_and_paywall(client):
    post = client.post("/api/posts", json={
        "title": "Issue 1", "subtitle": "first", "html_content": "<p>long content</p>" * 100,
        "visibility": "paid" }).json()
    slug = post["slug"]
    assert client.get(f"/blog/{slug}").status_code == 404  # still draft
    client.patch(f"/api/posts/{post['id']}", json={"status": "published"})
    page = client.get(f"/blog/{slug}")
    assert page.status_code == 200
    assert "This is a paid post" in page.text
    assert page.text.count("<p>long content</p>") < 100  # truncated preview, not the full body


def test_form_submit_creates_contact(client, audience):
    form = client.post("/api/forms", json={
        "name": "Signup", "audience_id": audience["id"], "tags": ["from-form"] }).json()
    resp = client.post(f"/f/{form['id']}/submit", data={"email": "formuser@example.com"})
    assert resp.status_code == 200
    assert "formuser@example.com" in resp.text
    contacts = client.get("/api/contacts").json()
    found = [c for c in contacts["contacts"] if c["email"] == "formuser@example.com"]
    assert found and "from-form" in found[0]["tags"]
    detail = client.get(f"/api/forms/{form['id']}").json()
    assert detail["submissions"] == 1


def test_public_unknown_page_404(client):
    assert client.get("/p/does-not-exist").status_code == 404


# ---------- growth ---------------------------------------------------------------------------

def test_referral_flow_and_leaderboard(client, audience):
    referrer = client.post("/api/contacts", json={
        "email": "ref@example.com", "audience_id": audience["id"] }).json()
    overview = client.get(f"/api/referrals/{referrer['id']}").json()
    code = overview["code"]
    # someone arrives via the referral link and subscribes
    assert client.get(f"/r/{code}").status_code in (200, 302, 307)
    contact = client.post("/api/contacts", json={
        "email": "referred@example.com", "audience_id": audience["id"] }).json()
    _ = contact  # direct signup through API path below
    form = client.post("/api/forms", json={
        "name": "f", "audience_id": audience["id"] }).json()
    client.post(f"/f/{form['id']}/submit", data={"email": "referred@example.com", "ref": code})
    overview = client.get(f"/api/referrals/{referrer['id']}").json()
    assert overview["signup_count"] == 1
    board = client.get("/api/referrals/leaderboard/top").json()
    assert board[0]["directs"] >= 1
    assert "referral-source" in [
        t for t in client.get(f"/api/contacts/{referrer['id']}").json()["tags"]]


def test_creator_network_recommendations(client):
    reco = client.post("/api/recommendations", json={
        "partner_name": "Indie Letters", "partner_url": "https://indie.example",
        "description": "Great newsletter" }).json()
    embed = client.post("/api/recommendations/embed").json()
    assert "Indie Letters" in embed["html"]
    client.get(f"/r/reco/{reco['id']}")  # redirect
    partners = client.get("/api/recommendations").json()
    assert partners[0]["clicks"] == 1
    assert partners[0]["impressions"] >= 1


def test_sponsorship_pipeline(client):
    slot = client.post("/api/sponsorships", json={
        "sponsor_name": "Acme Tool", "price_cents": 15000,
        "ad_copy": "Ship faster" }).json()
    sold = client.post(f"/api/sponsorships/{slot['id']}/book", json={
        "booked_by_email": "ads@acme.example" }).json()
    assert sold["status"] == "sold"
    client.post(f"/api/sponsorships/{slot['id']}/deliver")
    revenue = client.get("/api/sponsorships/revenue/summary").json()
    assert revenue["total_cents"] == 15000
    assert revenue["available_slots"] == 0


# ---------- integrations ----------------------------------------------------------------------

def test_webhook_registration_and_events(client):
    directory = client.get("/api/integrations/directory").json()
    assert "contact.created" in directory["webhook_events"]
    hook = client.post("/api/integrations/webhooks", json={
        "name": "Test hook", "url": "http://127.0.0.1:9/none", "events": ["contact.created"] })
    assert hook.status_code == 201
    bad = client.post("/api/integrations/webhooks", json={
        "name": "bad", "url": "http://x.example", "events": ["not.an.event"] })
    assert bad.status_code == 400
    assert client.delete(f"/api/integrations/webhooks/{hook.json()['id']}").status_code == 204


# ---------- analytics + AI chat -----------------------------------------------------------------

def test_analytics_overview_shape(client, audience, contacts):
    data = client.get("/api/analytics/overview").json()
    for key in ("audience", "engagement", "revenue", "funnel"):
        assert key in data
    assert data["audience"]["total_contacts"] == 3
    assert "view_to_signup_rate" in data["funnel"]


def test_ai_chat_without_key_returns_snapshot(client):
    resp = client.post("/api/agent/chat", json={"message": "How is growth going?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_powered"] is False
    assert "contacts" in body["reply"]


def test_ai_chat_empty_message_rejected(client):
    assert client.post("/api/agent/chat", json={"message": "  "}).status_code == 400
